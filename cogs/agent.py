import discord
from discord.ext import commands
from discord import app_commands, ui
import sqlite3
import os
from datetime import datetime
import re
from cogs.logger import log_slash_command
import asyncio
import mimetypes
import base64
import openai
from openai import OpenAI
from dotenv import load_dotenv
import time
import json

load_dotenv()

async def safe_defer(interaction: discord.Interaction):
    """安全的defer函数，避免重复响应"""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

class ToolConfirmView(ui.View):
    """工具调用确认视图"""
    def __init__(self, user_id: int, tool_calls: list, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.user_id = user_id  # 发起任务的用户ID
        self.tool_calls = tool_calls
        self.confirmed = None  # None: 等待中, True: 确认, False: 取消
        
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """检查交互用户是否是任务发起者"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "❌ 只有发起任务的用户才能操作这些按钮。",
                ephemeral=True
            )
            return False
        return True
    
    @ui.button(label="✅ 确认执行", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: ui.Button):
        await safe_defer(interaction)
        self.confirmed = True
        # 禁用所有按钮
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(view=self)
        self.stop()
    
    @ui.button(label="❌ 取消执行", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: ui.Button):
        await safe_defer(interaction)
        self.confirmed = False
        # 禁用所有按钮
        for item in self.children:
            item.disabled = True
        await interaction.edit_original_response(view=self)
        self.stop()
    
    async def on_timeout(self):
        """视图超时时的处理"""
        self.confirmed = False
        # 禁用所有按钮
        for item in self.children:
            item.disabled = True

class AgentCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        # 初始化Agent专用的OpenAI客户端
        agent_api_base = os.getenv("AGENT_MODEL_URL")
        agent_api_key = os.getenv("AGENT_MODEL_KEY")
        
        if agent_api_base and agent_api_key:
            self.openai_client = OpenAI(
                api_key=agent_api_key,
                base_url=agent_api_base
            )
            self.agent_model = os.getenv("AGENT_MODEL", "gemini-2.5-flash")
            print(f"✅ Agent OpenAI客户端已初始化: {agent_api_base}, 模型: {self.agent_model}")
        else:
            self.openai_client = None
            self.agent_model = None
            print("⚠️ Agent模型配置缺失，将无法使用Agent功能")
        
        # 加载配置
        self.agent_channel_id = os.getenv("AGENT_CHANNEL_ID", "")
        self.agent_role_ids = []
        
        # 确保agent_save文件夹存在
        os.makedirs('agent_save', exist_ok=True)
        
        # 解析身份组ID列表
        role_ids_str = os.getenv("AGENT_ROLE_IDS", "")
        if role_ids_str:
            try:
                self.agent_role_ids = [int(role_id.strip()) for role_id in role_ids_str.split(",") if role_id.strip()]
                print(f"✅ Agent功能已启用，监听频道: {self.agent_channel_id}, 允许身份组: {self.agent_role_ids}")
            except ValueError as e:
                print(f"❌ 解析AGENT_ROLE_IDS时出错: {e}")
        
        # 如果没有配置频道ID，禁用功能
        if not self.agent_channel_id:
            print("⚠️ 未配置AGENT_CHANNEL_ID，Agent功能将不会工作")
        else:
            try:
                self.agent_channel_id = int(self.agent_channel_id)
            except ValueError:
                print(f"❌ AGENT_CHANNEL_ID格式错误: {self.agent_channel_id}")
                self.agent_channel_id = None
        
        # 定义各模式的工具集
        self.mode_tools = {
            'search': {
                'get_context': self.tool_get_context,
                'search_user': self.tool_search_user,
                'get_user_info': self.tool_get_user_info,
                'mode': self.tool_mode_switch  # 模式切换工具
            },
            'debate': {
                'get_context': self.tool_get_context,
                'mode': self.tool_mode_switch
            },
            'ask': {
                'mode': self.tool_mode_switch
            },
            'execute': {
                'get_context': self.tool_get_context,
                'delete': self.tool_delete_messages,  # 删除消息工具只在execute模式可用
                'retake_exam': self.tool_retake_exam,  # 答题处罚工具只在execute模式可用
                'mode': self.tool_mode_switch
            }
        }
        
        # 工具描述（用于显示给用户）
        self.tool_descriptions = {
            'get_context': '获取频道历史消息上下文（最多100条，支持分页）',
            'search_user': '搜索指定用户的历史消息（支持批量获取）',
            'get_user_info': '获取Discord用户的详细信息（用户名、状态、身份组等）',
            'delete': '删除指定的Discord消息（最多5条，需要消息ID）',
            'retake_exam': '对指定用户执行答题处罚（需要用户ID）',
            'mode': '切换到不同的模式（search/debate/ask/execute）'
        }
        
        # 任务线状态跟踪
        self.active_tasks = {}  # 存储活跃的任务线
        
        # 用户当前模式跟踪
        self.user_modes = {}  # {user_id: 'mode_name'}
    
    def has_required_role(self, member: discord.Member) -> bool:
        """检查用户是否有所需的身份组"""
        if not self.agent_role_ids:
            return False
        
        member_role_ids = [role.id for role in member.roles]
        return any(role_id in member_role_ids for role_id in self.agent_role_ids)
    
    def is_user_registered(self, user_id):
        """检查用户是否已注册"""
        return user_id in self.bot.registered_users
    
    def deduct_quota_for_agent(self, user_id):
        """为Agent功能扣除用户配额"""
        # 管理员和受信任用户不受配额限制
        if user_id in self.bot.admins or user_id in self.bot.trusted_users:
            try:
                conn = sqlite3.connect('users.db')
                cursor = conn.cursor()
                cursor.execute("UPDATE users SET time = ? WHERE id = ?",
                             (datetime.now().isoformat(), str(user_id)))
                conn.commit()
                conn.close()
                # 同时更新内存中的数据
                user_data = next((user for user in self.bot.users_data if int(user['id']) == user_id), None)
                if user_data:
                    user_data['time'] = datetime.now().isoformat()
            except sqlite3.Error as e:
                print(f"[错误] 更新管理员/受信任用户时间时出错: {e}")
            return True

        # 对于普通用户，扣除配额
        try:
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            
            # 检查当前配额
            cursor.execute("SELECT quota FROM users WHERE id = ?", (str(user_id),))
            result = cursor.fetchone()
            
            if result and result[0] > 0:
                # 扣除配额并更新时间
                new_quota = result[0] - 1
                current_time = datetime.now().isoformat()
                cursor.execute("UPDATE users SET quota = ?, time = ? WHERE id = ?",
                             (new_quota, current_time, str(user_id)))
                conn.commit()
                
                # 同时更新内存中的数据
                user_data = next((user for user in self.bot.users_data if int(user['id']) == user_id), None)
                if user_data:
                    user_data['quota'] = new_quota
                    user_data['time'] = current_time
                
                conn.close()
                return True
            else:
                conn.close()
                return False
                
        except sqlite3.Error as e:
            print(f"[错误] 扣除配额时出错: {e}")
            return False
        
        return False
    
    def refund_quota_for_agent(self, user_id, amount=1):
        """为Agent功能返还用户配额"""
        # 管理员和受信任用户不受配额限制，因此无需返还
        if user_id in self.bot.admins or user_id in self.bot.trusted_users:
            return

        try:
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            
            # 获取当前配额并增加
            cursor.execute("SELECT quota FROM users WHERE id = ?", (str(user_id),))
            result = cursor.fetchone()
            
            if result:
                new_quota = result[0] + amount
                cursor.execute("UPDATE users SET quota = ? WHERE id = ?",
                             (new_quota, str(user_id)))
                conn.commit()
                
                # 同时更新内存中的数据
                user_data = next((user for user in self.bot.users_data if int(user['id']) == user_id), None)
                if user_data:
                    user_data['quota'] = new_quota
                
                print(f"配额已返还给用户 {user_id}，数量: {amount}。新配额: {new_quota}。")
            
            conn.close()
            
        except sqlite3.Error as e:
            print(f"[错误] 返还配额时出错: {e}")
    
    async def get_replied_message(self, message: discord.Message) -> str:
        """获取被回复的消息内容"""
        if message.reference and message.reference.message_id:
            try:
                # 获取被回复的消息
                replied_message = await message.channel.fetch_message(message.reference.message_id)
                return f"[被回复的消息] {replied_message.author.display_name}: {replied_message.content}\n\n"
            except discord.NotFound:
                return "[被回复的消息不可用]\n\n"
            except discord.HTTPException as e:
                print(f"获取被回复消息时出错: {e}")
                return ""
        return ""
    
    async def tool_get_context(self, params: str, channel: discord.TextChannel, current_message_id: int = None) -> str:
        """
        获取频道中的消息作为上下文
        参数格式: "数量" 或 "数量,起始位置"
        例如: "50" 获取最近50条，"50,100" 从第100条消息开始获取50条
        """
        try:
            # 解析参数
            parts = params.split(',') if params else ['20']
            n = min(int(parts[0]) if parts[0] else 20, 100)  # 限制最多获取100条消息
            offset = int(parts[1]) if len(parts) > 1 else 0  # 起始位置偏移
            
            messages = []
            message_count = 0
            skip_count = 0
            
            # 获取消息历史，但排除当前正在处理的消息
            # 为了处理偏移和过滤，需要获取更多消息
            limit = n + offset + 20  # 额外获取一些以补偿过滤
            
            async for msg in channel.history(limit=limit):
                # 跳过机器人自己的消息和当前消息
                if msg.author.bot or (current_message_id and msg.id == current_message_id):
                    continue
                
                # 处理偏移
                if skip_count < offset:
                    skip_count += 1
                    continue
                
                # 格式化消息：用户名: 内容
                msg_content = msg.content.strip()
                if msg_content:  # 只添加有内容的消息
                    messages.append(f"{msg.author.display_name}: {msg_content}")
                    message_count += 1
                
                if message_count >= n:
                    break
            
            messages.reverse()  # 反转顺序，使最早的消息在前
            
            if messages:
                # 添加上下文信息
                context_info = f"[历史消息上下文 - 共{len(messages)}条"
                if offset > 0:
                    context_info += f"，从第{offset+1}条开始"
                context_info += "]\n"
                
                context = context_info + "\n".join(messages) + "\n[上下文结束]\n"
                
                # 如果可能还有更多消息，添加提示
                if message_count >= n:
                    context += f"\n[提示: 可能还有更多历史消息，可使用 <get_context:{n},{offset+n}> 获取后续消息]\n"
                
                return context
            else:
                if offset > 0:
                    return f"[无可用的历史消息（从第{offset+1}条开始）]\n"
                else:
                    return "[无可用的历史消息]\n"
                
        except ValueError as e:
            return f"[参数错误: {e}。正确格式: <get_context:数量> 或 <get_context:数量,起始位置>]\n"
        except Exception as e:
            print(f"获取上下文时出错: {e}")
            return f"[获取上下文失败: {e}]\n"
    
    async def tool_search_user(self, params: str, channel: discord.TextChannel, current_message_id: int = None) -> str:
        """
        搜索指定用户的消息
        参数格式: "用户ID,消息数量"
        例如: "123456789,50" 获取用户123456789的最近50条消息
        如果消息数量为0，则获取所有消息（通过分批获取）
        """
        try:
            # 解析参数
            parts = params.split(',') if params else []
            if len(parts) < 2:
                return "[参数错误: 需要提供用户ID和消息数量，格式为 <search_user:用户ID,消息数量>]\n"
            
            try:
                user_id = int(parts[0].strip())
                message_count = int(parts[1].strip())
            except ValueError:
                return "[参数错误: 用户ID和消息数量必须是数字]\n"
            
            if message_count < 0:
                return "[参数错误: 消息数量不能为负数]\n"
            
            # 获取用户对象
            try:
                user = await self.bot.fetch_user(user_id)
                if not user:
                    return f"[错误: 找不到ID为 {user_id} 的用户]\n"
            except discord.NotFound:
                return f"[错误: 找不到ID为 {user_id} 的用户]\n"
            except discord.HTTPException as e:
                return f"[错误: 获取用户信息失败 - {e}]\n"
            
            messages = []
            total_fetched = 0
            batch_count = 0
            target_count = message_count if message_count > 0 else float('inf')
            
            print(f"🔍 开始搜索用户 {user.name} ({user_id}) 的消息，目标数量: {message_count if message_count > 0 else '全部'}")
            
            # 批量获取消息
            async for msg in channel.history(limit=None):
                # 只获取指定用户的消息（排除当前正在处理的消息）
                if msg.author.id == user_id and (not current_message_id or msg.id != current_message_id):
                    msg_content = msg.content.strip()
                    if msg_content:  # 只添加有内容的消息
                        # 格式化消息，包含时间戳
                        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        messages.append(f"[{timestamp}] {msg_content}")
                        total_fetched += 1
                        
                        if total_fetched >= target_count:
                            break
                
                # 每处理100条消息检查一次
                batch_count += 1
                if batch_count >= 100:
                    # 如果设置了获取所有消息（message_count=0），需要休息
                    if message_count == 0 and total_fetched > 0:
                        print(f"⏳ 已获取 {total_fetched} 条消息，休息5秒...")
                        await asyncio.sleep(5)
                    batch_count = 0
            
            # 反转消息顺序，使最早的消息在前
            messages.reverse()
            
            if messages:
                # 计算时间范围
                if len(messages) >= 2:
                    # 解析第一条和最后一条消息的时间
                    first_time_str = messages[0].split(']')[0][1:]
                    last_time_str = messages[-1].split(']')[0][1:]
                    
                    try:
                        first_time = datetime.strptime(first_time_str, "%Y-%m-%d %H:%M:%S")
                        last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S")
                        time_diff = last_time - first_time
                        days = time_diff.days
                        
                        # 构建上下文前缀
                        if days > 0:
                            context_prefix = f"[ID为{user_id}的用户的最近{days}天发言]\n"
                        else:
                            hours = time_diff.seconds // 3600
                            if hours > 0:
                                context_prefix = f"[ID为{user_id}的用户的最近{hours}小时发言]\n"
                            else:
                                context_prefix = f"[ID为{user_id}的用户的最近发言]\n"
                    except:
                        context_prefix = f"[ID为{user_id}的用户的最近发言]\n"
                else:
                    context_prefix = f"[ID为{user_id}的用户的最近发言]\n"
                
                # 构建完整的上下文
                context = context_prefix
                context += f"用户名: {user.name}\n"
                context += f"共找到 {len(messages)} 条消息\n"
                context += "-" * 50 + "\n"
                context += "\n".join(messages)
                context += "\n" + "-" * 50 + "\n"
                context += "[用户消息结束]\n"
                
                print(f"✅ 成功获取用户 {user.name} 的 {len(messages)} 条消息")
                return context
            else:
                return f"[未找到用户 {user.name} ({user_id}) 的任何消息]\n"
                
        except Exception as e:
            print(f"搜索用户消息时出错: {e}")
            return f"[搜索用户消息失败: {e}]\n"
    
    async def tool_delete_messages(self, params: str, channel: discord.TextChannel) -> str:
        """
        删除指定的Discord消息
        参数格式: "消息ID1,消息ID2,..." (最多5条)
        例如: "1413568628808487005,1413568854634004612"
        AI只需要提供消息ID的最后一部分，机器人会自动补全完整链接
        """
        try:
            # 解析参数
            if not params:
                return "[参数错误: 需要提供至少一个消息ID，格式为 <delete:消息ID1,消息ID2,...>]\n"
            
            # 分割消息ID
            message_ids_str = params.split(',')
            message_ids = []
            
            for id_str in message_ids_str:
                id_str = id_str.strip()
                if id_str:
                    try:
                        message_id = int(id_str)
                        message_ids.append(message_id)
                    except ValueError:
                        return f"[参数错误: 无效的消息ID '{id_str}'，必须是数字]\n"
            
            if not message_ids:
                return "[参数错误: 未提供有效的消息ID]\n"
            
            if len(message_ids) > 5:
                return f"[参数错误: 一次最多只能删除5条消息，您提供了{len(message_ids)}条]\n"
            
            # 删除结果统计
            success_count = 0
            failed_ids = []
            deleted_info = []
            
            print(f"🗑️ 开始删除 {len(message_ids)} 条消息...")
            
            # 并发删除消息
            delete_tasks = []
            for message_id in message_ids:
                delete_tasks.append(self._delete_single_message(channel, message_id))
            
            # 等待所有删除任务完成
            results = await asyncio.gather(*delete_tasks, return_exceptions=True)
            
            # 处理结果
            for i, (message_id, result) in enumerate(zip(message_ids, results)):
                if isinstance(result, Exception):
                    # 删除失败
                    failed_ids.append(str(message_id))
                    print(f"❌ 删除消息 {message_id} 失败: {result}")
                elif result is None:
                    # 消息不存在或无权限
                    failed_ids.append(str(message_id))
                    print(f"⚠️ 消息 {message_id} 不存在或无权限删除")
                else:
                    # 删除成功
                    success_count += 1
                    deleted_info.append(f"• ID {message_id}: {result}")
                    print(f"✅ 成功删除消息 {message_id}")
            
            # 如果删除了多条消息，添加冷却时间
            if success_count > 0 and len(message_ids) > 1:
                print(f"⏳ 批量删除完成，冷却3秒...")
                await asyncio.sleep(3)
            
            # 构建返回消息
            if success_count == len(message_ids):
                result_msg = f"[消息删除成功]\n"
                result_msg += f"成功删除 {success_count} 条消息\n"
                if deleted_info:
                    result_msg += "删除的消息：\n" + "\n".join(deleted_info[:10])  # 最多显示10条
                result_msg += "\n[删除操作完成]\n"
                return result_msg
            elif success_count > 0:
                result_msg = f"[部分消息删除成功]\n"
                result_msg += f"成功删除 {success_count}/{len(message_ids)} 条消息\n"
                if deleted_info:
                    result_msg += "成功删除的消息：\n" + "\n".join(deleted_info[:5]) + "\n"
                if failed_ids:
                    result_msg += f"删除失败的消息ID: {', '.join(failed_ids[:10])}\n"
                result_msg += "[删除操作完成]\n"
                return result_msg
            else:
                return f"[消息删除失败]\n所有消息都无法删除。可能原因：消息不存在、无权限或消息ID无效。\n失败的ID: {', '.join(failed_ids)}\n"
                
        except Exception as e:
            print(f"删除消息时出错: {e}")
            import traceback
            traceback.print_exc()
            return f"[删除消息失败: {e}]\n"
    
    async def _delete_single_message(self, channel: discord.TextChannel, message_id: int):
        """
        删除单条消息的辅助函数
        返回: 删除的消息摘要（成功）, None（失败）, 或 Exception（错误）
        """
        try:
            # 尝试获取消息
            message = await channel.fetch_message(message_id)
            
            # 保存消息摘要（用于日志）
            author_name = message.author.display_name
            content_preview = message.content[:50] + "..." if len(message.content) > 50 else message.content
            message_summary = f"{author_name}: {content_preview}"
            
            # 尝试删除消息
            await message.delete()
            
            return message_summary
            
        except discord.NotFound:
            # 消息不存在
            return None
        except discord.Forbidden:
            # 无权限删除
            return None
        except discord.HTTPException as e:
            # 其他HTTP错误
            return e
        except Exception as e:
            # 其他未知错误
            return e
    
    async def tool_get_user_info(self, params: str, guild: discord.Guild = None) -> str:
        """
        获取Discord用户的详细信息
        参数格式: "用户ID或用户名"
        例如: "123456789" 或 "username"
        """
        try:
            # 解析参数
            if not params:
                return "[参数错误: 需要提供用户ID或用户名，格式为 <get_user_info:用户ID或用户名>]\n"
            
            user_input = params.strip()
            user = None
            member = None
            
            # 尝试作为用户ID处理
            try:
                user_id = int(user_input)
                # 尝试获取用户对象
                try:
                    user = await self.bot.fetch_user(user_id)
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    print(f"通过ID获取用户失败: {e}")
                
                # 如果有guild，尝试获取成员对象
                if guild and user:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.NotFound:
                        pass
                    except discord.HTTPException:
                        pass
                        
            except ValueError:
                # 不是数字，尝试作为用户名搜索
                if guild:
                    # 在服务器成员中搜索
                    for m in guild.members:
                        if m.name.lower() == user_input.lower() or m.display_name.lower() == user_input.lower():
                            member = m
                            user = m
                            break
                    
                    # 如果还没找到，尝试模糊匹配
                    if not member:
                        for m in guild.members:
                            if user_input.lower() in m.name.lower() or user_input.lower() in m.display_name.lower():
                                member = m
                                user = m
                                break
            
            # 如果找不到用户
            if not user:
                return f"[错误: 找不到用户 '{user_input}']\n"
            
            # 构建用户信息
            info_lines = []
            info_lines.append(f"[Discord用户信息查询结果]")
            info_lines.append(f"")
            info_lines.append(f"**基本信息:**")
            info_lines.append(f"• 用户ID: {user.id}")
            info_lines.append(f"• 用户名: {user.name}")
            info_lines.append(f"• 显示名称: {user.display_name}")
            
            # 如果有成员信息（在服务器中）
            if member:
                if member.nick:
                    info_lines.append(f"• 服务器昵称: {member.nick}")
                else:
                    info_lines.append(f"• 服务器昵称: 无")
            
            info_lines.append(f"")
            info_lines.append(f"**账号信息:**")
            
            # 账号创建时间
            created_at = user.created_at
            created_at_str = created_at.strftime("%Y年%m月%d日 %H:%M:%S")
            days_since_creation = (datetime.now(created_at.tzinfo) - created_at).days
            info_lines.append(f"• 账号创建时间: {created_at_str} ({days_since_creation}天前)")
            
            # 加入服务器时间（如果有成员信息）
            if member and member.joined_at:
                joined_at = member.joined_at
                joined_at_str = joined_at.strftime("%Y年%m月%d日 %H:%M:%S")
                days_since_joined = (datetime.now(joined_at.tzinfo) - joined_at).days
                info_lines.append(f"• 加入服务器时间: {joined_at_str} ({days_since_joined}天前)")
            
            # 用户状态（如果有成员信息）
            if member:
                status_map = {
                    discord.Status.online: "🟢 在线",
                    discord.Status.idle: "🟡 闲置",
                    discord.Status.dnd: "🔴 请勿打扰",
                    discord.Status.offline: "⚫ 离线",
                    discord.Status.invisible: "⚫ 隐身"
                }
                status = status_map.get(member.status, "未知")
                info_lines.append(f"• 用户状态: {status}")
            
            # 用户头像URL
            if user.avatar:
                avatar_url = user.avatar.url
                info_lines.append(f"• 头像URL: {avatar_url}")
            else:
                info_lines.append(f"• 头像URL: 无自定义头像")
            
            # 是否为机器人
            info_lines.append(f"• 是否为机器人: {'是' if user.bot else '否'}")
            
            # 用户身份组（如果有成员信息）
            if member and member.roles:
                info_lines.append(f"")
                info_lines.append(f"**服务器身份组:**")
                # 过滤掉@everyone角色
                roles = [role for role in member.roles if role.name != "@everyone"]
                if roles:
                    # 按角色位置排序（高到低）
                    roles.sort(key=lambda r: r.position, reverse=True)
                    for role in roles[:10]:  # 最多显示10个角色
                        info_lines.append(f"• {role.name} (ID: {role.id})")
                    if len(roles) > 10:
                        info_lines.append(f"• ... 还有 {len(roles) - 10} 个身份组")
                else:
                    info_lines.append(f"• 无特殊身份组")
            
            # 添加结束标记
            info_lines.append(f"")
            info_lines.append(f"[用户信息查询结束]")
            
            return "\n".join(info_lines) + "\n"
            
        except Exception as e:
            print(f"获取用户信息时出错: {e}")
            import traceback
            traceback.print_exc()
            return f"[获取用户信息失败: {e}]\n"
    
    async def tool_retake_exam(self, params: str, channel: discord.TextChannel) -> str:
        """
        对特定用户执行答题处罚
        参数格式: "Discord用户ID"
        例如: "123456789"
        通过调用另一个机器人的斜杠命令 /答题处罚 来执行
        """
        try:
            # 解析参数
            if not params:
                return "[参数错误: 需要提供Discord用户ID，格式为 <retake_exam:用户ID>]\n"
            
            user_id_str = params.strip()
            
            # 验证是否为有效的数字ID
            try:
                user_id = int(user_id_str)
            except ValueError:
                return f"[参数错误: 无效的用户ID '{user_id_str}'，必须是数字]\n"
            
            # 验证用户是否存在
            try:
                user = await self.bot.fetch_user(user_id)
                if not user:
                    return f"[错误: 找不到ID为 {user_id} 的用户]\n"
            except discord.NotFound:
                return f"[错误: 找不到ID为 {user_id} 的用户]\n"
            except discord.HTTPException as e:
                return f"[错误: 获取用户信息失败 - {e}]\n"
            
            print(f"🔨 正在对用户 {user.name} ({user_id}) 执行答题处罚...")
            
            # 构建斜杠命令消息内容
            # 注意：Discord机器人无法直接调用其他机器人的斜杠命令
            # 这里发送一个格式化的消息，提示管理员手动执行或通过其他方式触发
            punishment_reason = "违反答疑规定"
            
            # 发送执行通知
            notification_msg = f"⚠️ **答题处罚执行通知**\n"
            notification_msg += f"目标用户: <@{user_id}> ({user.name})\n"
            notification_msg += f"处罚原因: {punishment_reason}\n"
            notification_msg += f"请管理员执行: `/答题处罚 @{user.name} {punishment_reason}`"
            
            # 在频道中发送通知
            await channel.send(notification_msg)
            
            # 记录日志
            print(f"✅ 已发送答题处罚通知: 用户 {user.name} ({user_id}), 原因: {punishment_reason}")
            
            # 返回执行结果
            result_msg = f"[答题处罚执行成功]\n"
            result_msg += f"目标用户: {user.name} (ID: {user_id})\n"
            result_msg += f"处罚原因: {punishment_reason}\n"
            result_msg += f"已在频道中发送处罚通知\n"
            result_msg += "[处罚执行完成]\n"
            
            return result_msg
            
        except Exception as e:
            print(f"执行答题处罚时出错: {e}")
            import traceback
            traceback.print_exc()
            return f"[答题处罚执行失败: {e}]\n"
    
    async def tool_mode_switch(self, params: str, user_id: int) -> str:
        """
        切换模式工具
        参数格式: "模式名称"
        例如: "search" 或 "debate" 或 "ask" 或 "execute"
        """
        mode = params.strip().lower()
        valid_modes = ['search', 'debate', 'ask', 'execute']
        
        if mode not in valid_modes:
            return f"[模式切换失败: 无效的模式 '{mode}'，可用模式: {', '.join(valid_modes)}]\n"
        
        # 更新用户模式
        self.user_modes[user_id] = mode
        
        return f"[模式已切换至: {mode}]\n"
    
    async def extract_tool_calls(self, ai_response: str) -> list:
        """从AI响应中提取工具调用"""
        tool_pattern = r'<(\w+):([^>]*)>'
        matches = re.finditer(tool_pattern, ai_response)
        tool_calls = []
        for match in matches:
            tool_calls.append({
                'name': match.group(1),
                'params': match.group(2),
                'full_match': match.group(0)
            })
        return tool_calls
    
    async def execute_tool_calls(self, tool_calls: list, message: discord.Message, user_mode: str) -> dict:
        """执行工具调用并返回结果"""
        results = {}
        user_id = message.author.id
        
        # 获取当前模式的工具集
        mode_tools = self.mode_tools.get(user_mode, {})
        
        for tool in tool_calls:
            tool_name = tool['name']
            params = tool['params']
            
            # 检查工具是否在当前模式中可用
            if tool_name in mode_tools:
                try:
                    # 根据不同的工具调用相应的函数
                    if tool_name in ['get_context', 'search_user']:
                        # 这些工具需要channel和message_id参数
                        result = await mode_tools[tool_name](params, message.channel, message.id)
                        results[tool['full_match']] = result
                    elif tool_name in ['delete', 'retake_exam']:
                        # delete和retake_exam工具只需要channel参数
                        result = await mode_tools[tool_name](params, message.channel)
                        results[tool['full_match']] = result
                    elif tool_name == 'get_user_info':
                        # get_user_info需要guild参数
                        result = await mode_tools[tool_name](params, message.guild)
                        results[tool['full_match']] = result
                    elif tool_name == 'mode':
                        # mode工具需要user_id参数
                        result = await mode_tools[tool_name](params, user_id)
                        results[tool['full_match']] = result
                    else:
                        # 其他工具可能有不同的参数需求
                        result = await mode_tools[tool_name](params)
                        results[tool['full_match']] = result
                except Exception as e:
                    print(f"执行工具 {tool_name} 时出错: {e}")
                    results[tool['full_match']] = f"[工具执行失败: {tool_name}]"
            else:
                # 生成更详细的错误提示，指出工具可用的模式
                available_modes = []
                for mode, tools in self.mode_tools.items():
                    if tool_name in tools:
                        available_modes.append(mode)
                
                if available_modes:
                    # 工具存在但在当前模式不可用
                    error_msg = f"[Tool Error: '{tool_name}' can only be used in {', '.join(available_modes)} mode(s). Current mode is '{user_mode}'.]"
                else:
                    # 工具不存在
                    error_msg = f"[Tool Error: Unknown tool '{tool_name}'. Available tools in '{user_mode}' mode: {', '.join(mode_tools.keys())}]"
                
                results[tool['full_match']] = error_msg
                print(f"⚠️ 工具调用被拒绝: {error_msg}")
        
        return results
    
    async def call_ai_api(self, messages):
        """调用Agent专用的OpenAI兼容API"""
        if not self.openai_client:
            raise Exception("Agent OpenAI客户端未初始化")
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.openai_client.chat.completions.create(
                model=self.agent_model,
                messages=messages,
                temperature=1.0,
                max_tokens=4096,
                stream=False
            )
        )
        
        return response.choices[0].message.content
    
    def save_prompt_to_file(self, user_id: int, message_id: int, prompt_content: str, mode: str):
        """保存完整的提示词到文件"""
        try:
            # 生成文件名：时间戳_用户ID_消息ID_模式.txt
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"agent_save/{timestamp}_{user_id}_{message_id}_{mode}.txt"
            
            # 准备要保存的内容
            save_content = {
                "timestamp": datetime.now().isoformat(),
                "user_id": user_id,
                "message_id": message_id,
                "mode": mode,
                "prompt": prompt_content
            }
            
            # 保存为格式化的文本文件
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(f"=== Agent 提示词记录 ===\n")
                f.write(f"时间: {save_content['timestamp']}\n")
                f.write(f"用户ID: {save_content['user_id']}\n")
                f.write(f"消息ID: {save_content['message_id']}\n")
                f.write(f"模式: {save_content['mode']}\n")
                f.write(f"{'='*50}\n\n")
                f.write("完整提示词:\n")
                f.write(f"{'='*50}\n")
                f.write(save_content['prompt'])
                f.write(f"\n{'='*50}\n")
            
            print(f"✅ 提示词已保存到: {filename}")
            return filename
        except Exception as e:
            print(f"❌ 保存提示词时出错: {e}")
            return None
    
    async def process_agent_request(self, message: discord.Message):
        """处理Agent请求的核心逻辑 - 使用任务线系统"""
        user_id = message.author.id
        task_id = f"{user_id}_{message.id}"
        
        # 检查Agent客户端是否已初始化
        if not self.openai_client:
            await message.reply("❌ Agent功能未正确配置，请联系管理员。", mention_author=True)
            return
        
        # 检查用户是否注册
        if not self.is_user_registered(user_id):
            await message.reply("❌ 您需要先使用 `/register` 命令注册才能使用Agent功能。", mention_author=True)
            return
        
        # Agent功能不受并发限制，但仍记录任务数用于监控
        # 注释掉并发限制检查
        # if self.bot.current_parallel_dayi_tasks >= int(os.getenv("MAX_PARALLEL", 5)):
        #     await message.reply("❌ 当前AI请求过多，请稍后再试。", mention_author=True)
        #     return
        
        # 扣除配额
        if not self.deduct_quota_for_agent(user_id):
            await message.reply("❌ 您的配额已用尽，无法使用Agent功能。", mention_author=True)
            return
        
        # 发送处理中消息
        processing_msg = await message.reply("⏳ 正在初始化任务线系统，请稍候...", mention_author=True)
        
        try:
            # Agent不增加并发计数，避免影响其他功能
            # self.bot.current_parallel_dayi_tasks += 1
            
            # 提取消息内容（移除机器人提及）
            text_content = message.content
            text_content = re.sub(f'<@!?{self.bot.user.id}>', '', text_content).strip()
            
            # 如果没有实际内容，使用默认提示
            if not text_content:
                text_content = "请帮助我"
            
            # 获取被回复的消息（如果有）
            replied_content = await self.get_replied_message(message)
            
            # 获取或设置用户的当前模式（默认为search）
            user_mode = self.user_modes.get(user_id, 'search')
            
            # 根据模式加载对应的提示词
            try:
                # 根据不同模式加载对应的提示词文件
                if user_mode == 'search':
                    with open('agent_prompt/search.txt', 'r', encoding='utf-8') as f:
                        prompt_head = f.read().strip()
                elif user_mode == 'debate':
                    with open('agent_prompt/debate_mode.txt', 'r', encoding='utf-8') as f:
                        prompt_head = f.read().strip()
                elif user_mode == 'ask':
                    with open('agent_prompt/ask_mode.txt', 'r', encoding='utf-8') as f:
                        prompt_head = f.read().strip()
                elif user_mode == 'execute':
                    with open('agent_prompt/execute_mode.txt', 'r', encoding='utf-8') as f:
                        prompt_head = f.read().strip()
                else:
                    # 未知模式，使用默认search模式
                    user_mode = 'search'
                    self.user_modes[user_id] = 'search'
                    with open('agent_prompt/search.txt', 'r', encoding='utf-8') as f:
                        prompt_head = f.read().strip()
            except FileNotFoundError as e:
                # 如果文件不存在，使用默认search模式
                print(f"⚠️ 提示词文件不存在: {e}，使用默认search模式")
                user_mode = 'search'
                self.user_modes[user_id] = 'search'
                with open('agent_prompt/search.txt', 'r', encoding='utf-8') as f:
                    prompt_head = f.read().strip()
            
            try:
                with open('agent_prompt/end.txt', 'r', encoding='utf-8') as f:
                    prompt_end = f.read().strip()
            except FileNotFoundError:
                prompt_end = "\n请提供详细且有帮助的回答。"
            
            # 初始化任务线系统
            task_context = []  # 存储任务执行的上下文
            max_iterations = 10  # 最大迭代次数，防止无限循环
            iteration = 0
            final_response = ""
            
            # 记录任务线状态
            self.active_tasks[task_id] = {
                'user_id': user_id,
                'message_id': message.id,
                'start_time': datetime.now(),
                'status': 'running',
                'iterations': 0
            }
            
            # 构建初始用户消息
            initial_user_message = prompt_head + "\n" + replied_content + text_content + prompt_end
            
            # 保存完整的提示词到文件
            self.save_prompt_to_file(user_id, message.id, initial_user_message, user_mode)
            
            # 任务线循环
            while iteration < max_iterations:
                iteration += 1
                self.active_tasks[task_id]['iterations'] = iteration
                print(f"🔄 Agent任务线 [{task_id}] - 迭代 {iteration}/{max_iterations}")
                
                # 构建消息列表
                messages = []
                
                if iteration == 1:
                    # 第一次迭代，使用初始消息
                    messages.append({"role": "user", "content": initial_user_message})
                else:
                    # 后续迭代，包含之前的上下文
                    messages.append({"role": "user", "content": initial_user_message})
                    for ctx in task_context:
                        messages.append({"role": "assistant", "content": ctx['response']})
                        if ctx.get('tool_results'):
                            messages.append({"role": "user", "content": ctx['tool_results']})
                
                # 调用AI API
                ai_response = await self.call_ai_api(messages)
                
                # 检查是否包含 <done> 标记
                if '<done>' in ai_response:
                    # 任务完成，移除 <done> 标记
                    final_response = ai_response.replace('<done>', '').strip()
                    self.active_tasks[task_id]['status'] = 'completed'
                    print(f"✅ Agent任务线 [{task_id}] 完成，共 {iteration} 次迭代")
                    break
                
                # 提取工具调用
                tool_calls = await self.extract_tool_calls(ai_response)
                
                if tool_calls:
                    # 构建工具调用说明
                    tool_info = []
                    for tool in tool_calls:
                        tool_name = tool['name']
                        tool_params = tool['params']
                        tool_desc = self.tool_descriptions.get(tool_name, '未知工具')
                        tool_info.append(f"• **{tool_name}**: {tool_desc}")
                        if tool_params:
                            tool_info.append(f"  参数: `{tool_params}`")
                    
                    # 清理AI响应中的工具调用标记
                    cleaned_response = re.sub(r'<\w+:[^>]*>', '', ai_response).strip()
                    
                    # 创建确认Embed
                    confirm_embed = discord.Embed(
                        title="🤖 AI 响应与工具调用确认",
                        color=discord.Color.blue()
                    )
                    
                    if cleaned_response:
                        confirm_embed.add_field(
                            name="AI 回复",
                            value=cleaned_response[:1024],  # Discord字段限制
                            inline=False
                        )
                    
                    confirm_embed.add_field(
                        name=f"需要执行 {len(tool_calls)} 个工具",
                        value="\n".join(tool_info[:10]),  # 最多显示10个工具
                        inline=False
                    )
                    
                    confirm_embed.add_field(
                        name="📍 当前进度",
                        value=f"迭代: {iteration}/{max_iterations}",
                        inline=True
                    )
                    
                    confirm_embed.set_footer(text="请确认是否执行这些工具调用（60秒超时）")
                    
                    # 创建确认视图
                    confirm_view = ToolConfirmView(user_id, tool_calls, timeout=60)
                    
                    # 更新消息显示确认界面
                    await processing_msg.edit(content="", embed=confirm_embed, view=confirm_view)
                    
                    # 等待用户确认
                    await confirm_view.wait()
                    
                    if confirm_view.confirmed is None:
                        # 超时
                        timeout_embed = discord.Embed(
                            title="⏱️ 操作超时",
                            description="工具调用确认已超时，任务已取消。",
                            color=discord.Color.orange()
                        )
                        await processing_msg.edit(embed=timeout_embed, view=confirm_view)
                        self.refund_quota_for_agent(user_id)
                        return
                    elif confirm_view.confirmed is False:
                        # 用户取消
                        cancel_embed = discord.Embed(
                            title="❌ 任务已取消",
                            description="您已取消工具调用，任务终止。",
                            color=discord.Color.red()
                        )
                        await processing_msg.edit(embed=cancel_embed, view=confirm_view)
                        self.refund_quota_for_agent(user_id)
                        return
                    else:
                        # 用户确认，执行工具调用
                        status_msg = f"⏳ 正在执行工具调用...\n📍 迭代: {iteration}/{max_iterations}\n🔧 执行 {len(tool_calls)} 个工具..."
                        
                        # 更新消息显示执行状态
                        executing_embed = discord.Embed(
                            title="🔧 执行中",
                            description=status_msg,
                            color=discord.Color.green()
                        )
                        await processing_msg.edit(embed=executing_embed, view=None)
                        
                        # 执行工具调用，传入当前模式
                        tool_results = await self.execute_tool_calls(tool_calls, message, user_mode)
                        
                        # 检查是否有模式切换
                        for tool_match, result in tool_results.items():
                            if 'mode:' in tool_match and '模式已切换至' in result:
                                # 更新当前模式
                                user_mode = self.user_modes.get(user_id, 'search')
                                print(f"🔄 用户 {user_id} 切换到模式: {user_mode}")
                        
                        # 构建工具结果消息
                        tool_results_message = "工具执行结果：\n"
                        for tool_match, result in tool_results.items():
                            tool_results_message += f"{tool_match} -> {result}\n"
                        
                        # 保存上下文
                        task_context.append({
                            'response': ai_response,
                            'tool_results': tool_results_message
                        })
                        
                        print(f"🔧 执行了 {len(tool_calls)} 个工具调用")
                else:
                    # 没有工具调用，保存响应并继续
                    task_context.append({
                        'response': ai_response,
                        'tool_results': None
                    })
                    
                    # 如果AI没有明确标记完成，但也没有工具调用，可能需要提示
                    if iteration >= 3:  # 给AI几次机会
                        # 添加提示让AI完成任务
                        task_context.append({
                            'response': "请基于已有信息完成任务，如果任务已完成，请输出 <done> 标记。",
                            'tool_results': None
                        })
            
            # 如果达到最大迭代次数仍未完成
            if iteration >= max_iterations:
                self.active_tasks[task_id]['status'] = 'max_iterations_reached'
                final_response = "⚠️ 任务执行超过最大迭代次数，以下是部分结果：\n\n"
                # 合并所有有意义的响应
                for ctx in task_context:
                    response = ctx['response']
                    # 过滤掉纯工具调用的响应
                    if not (response.startswith('<') and response.endswith('>')):
                        # 清理响应中的工具调用标记
                        cleaned = re.sub(r'<\w+:[^>]*>', '', response).strip()
                        if cleaned:
                            final_response += cleaned + "\n\n"
            
            # 如果响应太长，分割成多条消息
            if len(final_response) > 2000:
                # 分割消息
                chunks = []
                current_chunk = ""
                
                for line in final_response.split('\n'):
                    if len(current_chunk) + len(line) + 1 > 1900:  # 留出一些空间
                        chunks.append(current_chunk)
                        current_chunk = line
                    else:
                        current_chunk += '\n' + line if current_chunk else line
                
                if current_chunk:
                    chunks.append(current_chunk)
                
                # 发送第一条消息（编辑原消息）
                if chunks:
                    embed = discord.Embed(
                        title="🤖 Agent 回复（任务线完成）",
                        description=chunks[0],
                        color=discord.Color.blue()
                    )
                    embed.set_footer(text=f"由 {self.agent_model} 提供支持 | 迭代 {iteration} 次 | 消息 1/{len(chunks)}")
                    await processing_msg.edit(content="", embed=embed)
                    
                    # 发送剩余的消息
                    for i, chunk in enumerate(chunks[1:], 2):
                        embed = discord.Embed(
                            description=chunk,
                            color=discord.Color.blue()
                        )
                        embed.set_footer(text=f"消息 {i}/{len(chunks)}")
                        await message.channel.send(embed=embed)
            else:
                # 创建并发送回复
                embed = discord.Embed(
                    title="🤖 Agent 回复（任务线完成）",
                    description=final_response,
                    color=discord.Color.blue()
                )
                embed.set_footer(text=f"由 {self.agent_model} 提供支持 | 迭代 {iteration} 次")
                await processing_msg.edit(content="", embed=embed)
            
            # 清理任务线记录
            if task_id in self.active_tasks:
                elapsed_time = (datetime.now() - self.active_tasks[task_id]['start_time']).total_seconds()
                print(f"✅ Agent成功处理用户 {user_id} 的消息 - 耗时 {elapsed_time:.2f}秒，迭代 {iteration} 次")
                del self.active_tasks[task_id]
            
        except openai.APIConnectionError as e:
            await processing_msg.edit(content=f"❌ **连接错误**: 无法连接到AI服务。\n`{e}`")
            self.refund_quota_for_agent(user_id)
        except openai.RateLimitError as e:
            await processing_msg.edit(content=f"❌ **请求超速**: 已达到API的请求频率限制。\n`{e}`")
            self.refund_quota_for_agent(user_id)
        except openai.AuthenticationError as e:
            await processing_msg.edit(content=f"❌ **认证失败**: API密钥无效或已过期。\n`{e}`")
            self.refund_quota_for_agent(user_id)
        except openai.APIStatusError as e:
            await processing_msg.edit(content=f"❌ **API 错误**: API返回了非200的状态码。\n状态码: {e.status_code}")
            self.refund_quota_for_agent(user_id)
        except Exception as e:
            print(f"[Agent错误] 调用AI时发生错误: {type(e).__name__} - {e}")
            await processing_msg.edit(content=f"❌ 发生意外错误: {e}，请联系管理员。")
            self.refund_quota_for_agent(user_id)
        
        finally:
            # Agent不计入并发数，所以不需要减少
            # self.bot.current_parallel_dayi_tasks -= 1
            # 确保清理任务线记录
            if task_id in self.active_tasks:
                del self.active_tasks[task_id]
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """监听消息事件"""
        # 忽略机器人自己的消息
        if message.author.bot:
            return
        
        # 检查是否启用了Agent功能
        if not self.agent_channel_id or not self.agent_role_ids:
            return
        
        # 检查是否在指定频道
        if message.channel.id != self.agent_channel_id:
            return
        
        # 检查是否提及了机器人
        if not (self.bot.user.mentioned_in(message) or f"<@{self.bot.user.id}>" in message.content or f"<@!{self.bot.user.id}>" in message.content):
            return
        
        # 检查用户是否有所需的身份组
        if not isinstance(message.author, discord.Member):
            return
        
        if not self.has_required_role(message.author):
            await message.reply("❌ 您没有权限使用Agent功能。", mention_author=True)
            return
        
        print(f"🤖 Agent: 检测到用户 {message.author.name} ({message.author.id}) 的请求")
        
        # 处理Agent请求
        await self.process_agent_request(message)
    
    @app_commands.command(name='agent_status', description='[仅管理员] 查看Agent功能状态')
    async def agent_status(self, interaction: discord.Interaction):
        """查看Agent功能的状态"""
        # 检查权限
        if interaction.user.id not in self.bot.admins:
            await interaction.response.send_message('❌ 您没有权限使用此命令。', ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🤖 Agent功能状态",
            color=discord.Color.blue()
        )
        
        # 检查配置状态
        if self.agent_channel_id and self.agent_role_ids:
            channel = self.bot.get_channel(self.agent_channel_id)
            channel_info = f"{channel.mention} (`{self.agent_channel_id}`)" if channel else f"未知频道 (`{self.agent_channel_id}`)"
            
            # 获取身份组信息
            guild = interaction.guild
            role_info = []
            for role_id in self.agent_role_ids:
                role = guild.get_role(role_id) if guild else None
                if role:
                    role_info.append(f"• {role.mention} (`{role_id}`)")
                else:
                    role_info.append(f"• 未知身份组 (`{role_id}`)")
            
            embed.add_field(
                name="状态",
                value="✅ 已启用",
                inline=True
            )
            embed.add_field(
                name="监听频道",
                value=channel_info,
                inline=True
            )
            # 计算Agent任务数（不计入总并发）
            agent_task_count = len(self.active_tasks)
            embed.add_field(
                name="Agent任务/总并发",
                value=f"{agent_task_count}/{self.bot.current_parallel_dayi_tasks}",
                inline=True
            )
            embed.add_field(
                name="允许的身份组",
                value="\n".join(role_info) if role_info else "无",
                inline=False
            )
            
            # 显示所有模式和工具
            mode_info = []
            for mode, tools in self.mode_tools.items():
                tool_list = ", ".join(f"`{tool}`" for tool in tools.keys())
                mode_info.append(f"**{mode}模式**: {tool_list}")
            
            embed.add_field(
                name="可用模式和工具",
                value="\n".join(mode_info),
                inline=False
            )
            
            # 显示活跃的任务线
            if self.active_tasks:
                active_info = []
                for task_id, task_data in self.active_tasks.items():
                    elapsed = (datetime.now() - task_data['start_time']).total_seconds()
                    active_info.append(f"• 任务 {task_id}: 迭代 {task_data['iterations']}次, 耗时 {elapsed:.1f}秒")
                
                embed.add_field(
                    name="活跃任务线",
                    value="\n".join(active_info[:5]),  # 最多显示5个
                    inline=False
                )
        else:
            embed.add_field(
                name="状态",
                value="❌ 未启用",
                inline=False
            )
            
            missing_configs = []
            if not self.agent_channel_id:
                missing_configs.append("• AGENT_CHANNEL_ID")
            if not self.agent_role_ids:
                missing_configs.append("• AGENT_ROLE_IDS")
            
            embed.add_field(
                name="缺少的配置",
                value="\n".join(missing_configs),
                inline=False
            )
            embed.add_field(
                name="配置说明",
                value="请在 .env 文件中设置缺少的环境变量",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(AgentCog(bot))