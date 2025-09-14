import discord
from discord.ext import commands
from discord import app_commands
import os
import sqlite3
import asyncio
from datetime import datetime, timedelta
import json
from typing import Optional, List, Dict, Tuple
import aiofiles
from dotenv import load_dotenv
import io

# 加载环境变量
load_dotenv()

class QuickPunishModal(discord.ui.Modal):
    """快速处罚确认表单"""
    
    def __init__(self, target_message: discord.Message, cog):
        super().__init__(title=f"快速处罚 - {target_message.author.display_name}")
        self.target_message = target_message
        self.target_user = target_message.author
        self.cog = cog
        
        # 原因输入框（最多100字符）
        self.reason = discord.ui.TextInput(
            label="处罚原因",
            placeholder="请输入处罚原因（留空则使用默认值'违规提问行为'）",
            required=False,
            max_length=100,
            style=discord.TextStyle.short
        )
        
        # 确认输入框（需要输入用户名或ID）
        self.confirmation = discord.ui.TextInput(
            label=f"确认处罚（输入用户名或ID）",
            placeholder=f"用户名: {target_message.author.name} 或 ID: {target_message.author.id}",
            required=True,
            max_length=100,
            style=discord.TextStyle.short
        )
        
        self.add_item(self.reason)
        self.add_item(self.confirmation)
    
    def validate_confirmation(self, confirmation_input: str) -> bool:
        """验证用户输入的确认信息"""
        confirmation = confirmation_input.strip()
        return (confirmation == self.target_user.name or 
                confirmation == str(self.target_user.id))
    
    async def safe_defer(self, interaction: discord.Interaction):
        """安全的defer响应"""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        """处理表单提交"""
        # 立即defer以避免超时
        await self.safe_defer(interaction)
        
        # 验证确认信息
        if not self.validate_confirmation(self.confirmation.value):
            await interaction.followup.send(
                "❌ 确认信息不匹配，操作已取消。", 
                ephemeral=True
            )
            return
        
        # 获取处罚原因
        reason = self.reason.value.strip() or "违规提问行为"
        
        # 执行处罚
        success, message = await self.cog.execute_punishment(
            interaction=interaction,
            target_user=self.target_user,
            target_message=self.target_message,
            reason=reason,
            executor=interaction.user
        )
        
        if success:
            await interaction.followup.send(
                f"✅ 处罚执行成功\n{message}", 
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ 处罚执行失败\n{message}", 
                ephemeral=True
            )
    
    async def on_error(self, interaction: discord.Interaction, error: Exception):
        """处理错误"""
        print(f"QuickPunishModal错误: {error}")
        try:
            await self.safe_defer(interaction)
            await interaction.followup.send(
                f"❌ 发生错误：{str(error)}", 
                ephemeral=True
            )
        except:
            pass


class QuickPunishCog(commands.Cog):
    """快速处罚功能Cog"""
    
    def __init__(self, bot):
        self.bot = bot
        self.init_database()
        
        # 从环境变量加载配置
        self.enabled = os.getenv("QUICK_PUNISH_ENABLED", "false").lower() == "true"
        self.allowed_roles = self._parse_role_ids(os.getenv("QUICK_PUNISH_ROLES", ""))
        self.remove_roles = self._parse_role_ids(os.getenv("QUICK_PUNISH_REMOVE_ROLES", ""))
        self.log_channel_id = self._parse_channel_id(os.getenv("QUICK_PUNISH_LOG_CHANNEL"))
        self.interface_channel_id = self._parse_channel_id(os.getenv("QUICK_PUNISH_INTERFACE_CHANNEL"))
        self.appeal_channel_id = self._parse_channel_id(os.getenv("QUICK_PUNISH_APPEAL_CHANNEL"))
    
    def _parse_role_ids(self, role_str: str) -> List[int]:
        """解析身份组ID字符串"""
        if not role_str:
            return []
        try:
            return [int(role_id.strip()) for role_id in role_str.split(",") if role_id.strip()]
        except ValueError:
            print(f"警告：无法解析身份组ID: {role_str}")
            return []
    
    def _parse_channel_id(self, channel_str: str) -> Optional[int]:
        """解析频道ID字符串"""
        if not channel_str:
            return None
        try:
            return int(channel_str.strip())
        except ValueError:
            print(f"警告：无法解析频道ID: {channel_str}")
            return None
    
    def init_database(self):
        """初始化数据库"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS quick_punish_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                user_name TEXT NOT NULL,
                punish_count INTEGER DEFAULT 1,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                original_message_id TEXT,
                original_message_link TEXT,
                channel_id TEXT,
                channel_name TEXT,
                executor_id TEXT NOT NULL,
                executor_name TEXT NOT NULL,
                reason TEXT,
                removed_roles TEXT,
                status TEXT DEFAULT 'executed'
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def has_permission(self, interaction: discord.Interaction) -> bool:
        """检查用户是否有快速处罚权限"""
        if not self.enabled:
            return False
        
        if not self.allowed_roles:
            return False
        
        user_roles = [role.id for role in interaction.user.roles]
        return any(role_id in user_roles for role_id in self.allowed_roles)
    
    async def get_punish_count(self, user_id: str) -> int:
        """获取用户被处罚次数"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT COUNT(*) FROM quick_punish_records WHERE user_id = ? AND status = 'executed'",
            (user_id,)
        )
        count = cursor.fetchone()[0]
        
        conn.close()
        return count
    
    async def send_dm(self, user: discord.User, message_content: str) -> bool:
        """发送私信给用户"""
        try:
            await user.send(message_content)
            return True
        except discord.Forbidden:
            print(f"无法发送私信给用户 {user.name} ({user.id})")
            return False
        except Exception as e:
            print(f"发送私信时出错: {e}")
            return False
    
    async def remove_user_roles(self, member: discord.Member, roles_to_remove: List[int]) -> List[int]:
        """移除用户的身份组，返回实际被移除的身份组ID列表"""
        removed_roles = []
        roles_to_remove_objs = []
        
        for role_id in roles_to_remove:
            role = member.guild.get_role(role_id)
            if role and role in member.roles:
                roles_to_remove_objs.append(role)
                removed_roles.append(role_id)
        
        if roles_to_remove_objs:
            try:
                await member.remove_roles(*roles_to_remove_objs, reason="快速处罚")
            except Exception as e:
                print(f"移除身份组时出错: {e}")
                raise
        
        return removed_roles
    
    async def log_to_database(self, user: discord.User, message: discord.Message,
                             executor: discord.User, reason: str, removed_roles: List[int],
                             status: str = "executed") -> int:
        """记录处罚信息到数据库"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        
        message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
        
        cursor.execute('''
            INSERT INTO quick_punish_records 
            (user_id, user_name, timestamp, original_message_id, original_message_link,
             channel_id, channel_name, executor_id, executor_name, reason, removed_roles, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            str(user.id),
            user.name,
            datetime.now().isoformat(),
            str(message.id),
            message_link,
            str(message.channel.id),
            message.channel.name,
            str(executor.id),
            executor.name,
            reason,
            json.dumps(removed_roles),
            status
        ))
        
        record_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return record_id
    
    async def send_log_embed(self, channel: discord.TextChannel, user: discord.User,
                            executor: discord.User, reason: str, message_link: str,
                            removed_roles: List[int], record_id: int,
                            original_message: discord.Message = None):
        """发送日志Embed到指定频道，并转发原消息"""
        embed = discord.Embed(
            title="⚠️ 快速处罚执行",
            color=discord.Color.red(),
            timestamp=datetime.now()
        )
        
        embed.add_field(name="处罚对象", value=f"{user.mention} ({user.id})", inline=False)
        embed.add_field(name="执行者", value=f"{executor.mention}", inline=True)
        embed.add_field(name="原因", value=reason, inline=True)
        embed.add_field(name="原消息", value=f"[跳转到消息]({message_link})", inline=False)
        
        if removed_roles:
            guild = channel.guild
            roles_str = ", ".join([f"<@&{role_id}>" for role_id in removed_roles])
            embed.add_field(name="移除的身份组", value=roles_str, inline=False)
        
        embed.set_footer(text=f"记录ID: {record_id}")
        
        try:
            # 发送日志Embed
            await channel.send(embed=embed)
            
            # 尝试转发原消息
            if original_message:
                await self._forward_original_message(channel, original_message, user)
        except Exception as e:
            print(f"发送日志Embed时出错: {e}")
    
    async def _forward_original_message(self, channel: discord.TextChannel,
                                       message: discord.Message,
                                       punished_user: discord.User):
        """转发被处罚的原消息到日志频道"""
        try:
            # 检查消息是否仍然存在
            try:
                # 尝试重新获取消息，确保它仍然存在
                fresh_message = await message.channel.fetch_message(message.id)
            except (discord.NotFound, discord.HTTPException):
                # 消息已被删除
                fallback_embed = discord.Embed(
                    title="📝 原消息内容（已删除）",
                    description="*消息已被删除，无法获取内容*",
                    color=discord.Color.greyple()
                )
                fallback_embed.add_field(
                    name="消息信息",
                    value=f"作者: {punished_user.mention}\n"
                          f"频道: <#{message.channel.id}>\n"
                          f"消息ID: {message.id}",
                    inline=False
                )
                await channel.send(embed=fallback_embed)
                return
            
            # 构建转发的Embed
            forward_embed = discord.Embed(
                title="📝 被处罚的原消息",
                color=discord.Color.dark_grey(),
                timestamp=fresh_message.created_at
            )
            
            # 添加作者信息
            forward_embed.set_author(
                name=f"{fresh_message.author.display_name} (@{fresh_message.author.name})",
                icon_url=fresh_message.author.avatar.url if fresh_message.author.avatar else None
            )
            
            # 添加消息内容
            content = fresh_message.content[:4000] if fresh_message.content else "*无文本内容*"
            if len(fresh_message.content) > 4000:
                content += "\n...*内容过长已截断*"
            forward_embed.add_field(name="消息内容", value=content, inline=False)
            
            # 添加频道和时间信息
            forward_embed.add_field(
                name="位置",
                value=f"频道: <#{fresh_message.channel.id}>\n"
                      f"[跳转到原消息](https://discord.com/channels/{fresh_message.guild.id}/{fresh_message.channel.id}/{fresh_message.id})",
                inline=False
            )
            
            # 如果有附件，添加附件信息
            if fresh_message.attachments:
                attachments_info = []
                for att in fresh_message.attachments[:5]:  # 最多显示5个附件
                    attachments_info.append(f"• [{att.filename}]({att.url})")
                if len(fresh_message.attachments) > 5:
                    attachments_info.append(f"*...还有 {len(fresh_message.attachments) - 5} 个附件*")
                forward_embed.add_field(
                    name=f"附件 ({len(fresh_message.attachments)})",
                    value="\n".join(attachments_info),
                    inline=False
                )
            
            # 如果有嵌入（Embeds），添加说明
            if fresh_message.embeds:
                forward_embed.add_field(
                    name="嵌入内容",
                    value=f"*包含 {len(fresh_message.embeds)} 个嵌入内容*",
                    inline=False
                )
            
            # 如果有贴纸（Stickers），添加贴纸信息
            if fresh_message.stickers:
                stickers_info = ", ".join([sticker.name for sticker in fresh_message.stickers])
                forward_embed.add_field(
                    name="贴纸",
                    value=stickers_info,
                    inline=False
                )
            
            await channel.send(embed=forward_embed)
            
        except Exception as e:
            print(f"转发原消息时出错: {e}")
            # 发送错误信息
            error_embed = discord.Embed(
                title="⚠️ 无法转发原消息",
                description=f"转发消息时发生错误：{str(e)}",
                color=discord.Color.orange()
            )
            await channel.send(embed=error_embed)
    
    async def execute_punishment(self, interaction: discord.Interaction, 
                                target_user: discord.User,
                                target_message: discord.Message,
                                reason: str,
                                executor: discord.User) -> tuple[bool, str]:
        """执行处罚的主要逻辑"""
        guild = interaction.guild
        
        # 检查用户是否在服务器中
        member = guild.get_member(target_user.id)
        if not member:
            return False, "用户不在服务器中"
        
        # 检查用户是否拥有需要移除的身份组
        user_role_ids = [role.id for role in member.roles]
        roles_to_remove = [role_id for role_id in self.remove_roles if role_id in user_role_ids]
        
        if not roles_to_remove:
            return False, "用户未拥有需要移除的身份组，操作已取消"
        
        # 获取用户被处罚次数
        punish_count = await self.get_punish_count(str(target_user.id)) + 1
        
        # 构建私信内容
        dm_content = await self._build_dm_content(
            target_message=target_message,
            reason=reason,
            executor=executor,
            punish_count=punish_count
        )
        
        # 发送私信（失败不影响后续流程）
        dm_sent = await self.send_dm(target_user, dm_content)
        
        try:
            # 移除身份组
            removed_roles = await self.remove_user_roles(member, roles_to_remove)
            
            # 记录到数据库
            record_id = await self.log_to_database(
                user=target_user,
                message=target_message,
                executor=executor,
                reason=reason,
                removed_roles=removed_roles,
                status="executed"
            )
            
            # 发送通知到原频道
            await self._send_channel_notification(
                channel=target_message.channel,
                user=target_user,
                executor=executor,
                reason=reason,
                removed_roles=removed_roles
            )
            
            # 发送到日志频道
            if self.log_channel_id:
                log_channel = guild.get_channel(self.log_channel_id)
                if log_channel:
                    message_link = f"https://discord.com/channels/{guild.id}/{target_message.channel.id}/{target_message.id}"
                    await self.send_log_embed(
                        channel=log_channel,
                        user=target_user,
                        executor=executor,
                        reason=reason,
                        message_link=message_link,
                        removed_roles=removed_roles,
                        record_id=record_id,
                        original_message=target_message
                    )
            
            # 发送到对接频道
            if self.interface_channel_id:
                interface_channel = guild.get_channel(self.interface_channel_id)
                if interface_channel:
                    await interface_channel.send(f'{{"qp": {target_user.id}}}')
            
            success_msg = f"用户 {target_user.mention} 已被处罚"
            if not dm_sent:
                success_msg += "\n⚠️ 注意：私信发送失败（用户可能关闭了私信）"
            
            return True, success_msg
            
        except Exception as e:
            print(f"执行处罚时出错: {e}")
            # 记录失败状态
            await self.log_to_database(
                user=target_user,
                message=target_message,
                executor=executor,
                reason=reason,
                removed_roles=[],
                status="failed"
            )
            return False, f"执行处罚时出错：{str(e)}"
    
    async def _build_dm_content(self, target_message: discord.Message, 
                               reason: str, executor: discord.User, 
                               punish_count: int) -> str:
        """构建私信内容"""
        # 读取3rd.txt文件内容
        third_content = "请重新完成新人验证答题。"  # 默认内容
        try:
            async with aiofiles.open('rag_prompt/3rd.txt', 'r', encoding='utf-8') as f:
                third_content = await f.read()
        except Exception as e:
            print(f"读取3rd.txt文件失败: {e}")
        
        # 构建完整私信
        dm_parts = [
            "===== 答题处罚通知 =====\n",
            f"你已被要求重新答题。",
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"频道：#{target_message.channel.name}",
            f"原因：{reason}",
            f"执行者：{executor.name}\n",
            third_content.strip(),
            f"\n这是你第{punish_count}次被答题处罚。请仔细阅读社区规则，重新完成新人验证答题。"
        ]
        
        # 添加申诉信息
        if self.appeal_channel_id:
            dm_parts.append(f"\n如有异议，请私信服务器管理员。")
        
        return "\n".join(dm_parts)
    
    async def _send_channel_notification(self, channel: discord.TextChannel,
                                        user: discord.User, executor: discord.User,
                                        reason: str, removed_roles: List[int]):
        """在原频道发送处罚通知"""
        embed = discord.Embed(
            title="⚠️ 快速处罚",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        
        embed.add_field(name="处罚对象", value=f"{user.mention}", inline=True)
        embed.add_field(name="执行者", value=f"{executor.mention}", inline=True)
        embed.add_field(name="原因", value=reason, inline=False)
        
        if removed_roles:
            roles_str = ", ".join([f"<@&{role_id}>" for role_id in removed_roles])
            embed.add_field(name="已移除身份组", value=roles_str, inline=False)
        
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"发送频道通知时出错: {e}")
    
    async def get_recent_punishments(self, count: int = 3, max_count: int = 1000) -> List[Dict]:
        """获取最近的处罚记录"""
        # 确保count在合理范围内
        count = min(count, max_count)
        count = max(count, 1)
        
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, user_id, user_name, timestamp, channel_name,
                   executor_name, reason, removed_roles, status
            FROM quick_punish_records
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (count,))
        
        records = []
        for row in cursor.fetchall():
            records.append({
                'id': row[0],
                'user_id': row[1],
                'user_name': row[2],
                'timestamp': row[3],
                'channel_name': row[4],
                'executor_name': row[5],
                'reason': row[6],
                'removed_roles': json.loads(row[7]) if row[7] else [],
                'status': row[8]
            })
        
        conn.close()
        return records
    
    async def format_punishment_records(self, records: List[Dict], guild: discord.Guild) -> str:
        """格式化处罚记录为文本"""
        if not records:
            return "暂无处罚记录"
        
        lines = ["===== 快速处罚记录 =====\n"]
        
        for i, record in enumerate(records, 1):
            # 解析时间
            try:
                timestamp = datetime.fromisoformat(record['timestamp'])
                time_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
            except:
                time_str = record['timestamp']
            
            # 格式化身份组
            roles_str = "无"
            if record['removed_roles']:
                role_names = []
                for role_id in record['removed_roles']:
                    role = guild.get_role(role_id)
                    if role:
                        role_names.append(f"@{role.name}")
                    else:
                        role_names.append(f"ID:{role_id}")
                roles_str = ", ".join(role_names)
            
            # 构建记录文本
            lines.append(f"【记录 #{i}】")
            lines.append(f"记录ID: {record['id']}")
            lines.append(f"用户: {record['user_name']} (ID: {record['user_id']})")
            lines.append(f"时间: {time_str}")
            lines.append(f"频道: #{record['channel_name']}")
            lines.append(f"执行者: {record['executor_name']}")
            lines.append(f"原因: {record['reason']}")
            lines.append(f"移除身份组: {roles_str}")
            lines.append(f"状态: {record['status']}")
            lines.append("-" * 50 + "\n")
        
        return "\n".join(lines)
    
    async def get_last_punishment_for_user(self, user_id: str) -> Optional[Dict]:
        """获取用户最近一次的处罚记录"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, user_id, user_name, timestamp, original_message_id,
                   original_message_link, channel_id, channel_name,
                   executor_id, executor_name, reason, removed_roles, status
            FROM quick_punish_records
            WHERE user_id = ? AND status = 'executed'
            ORDER BY timestamp DESC
            LIMIT 1
        ''', (user_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
        
        return {
            'id': row[0],
            'user_id': row[1],
            'user_name': row[2],
            'timestamp': row[3],
            'original_message_id': row[4],
            'original_message_link': row[5],
            'channel_id': row[6],
            'channel_name': row[7],
            'executor_id': row[8],
            'executor_name': row[9],
            'reason': row[10],
            'removed_roles': json.loads(row[11]) if row[11] else [],
            'status': row[12]
        }
    
    async def revoke_punishment(self, record_id: int) -> bool:
        """撤销处罚记录（更新状态为revoked）"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE quick_punish_records
            SET status = 'revoked'
            WHERE id = ? AND status = 'executed'
        ''', (record_id,))
        
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        return affected > 0
    
    async def restore_user_roles(self, member: discord.Member, roles_to_restore: List[int]) -> Tuple[List[int], List[int]]:
        """恢复用户的身份组
        返回: (成功恢复的身份组ID列表, 失败的身份组ID列表)
        """
        restored_roles = []
        failed_roles = []
        roles_to_add = []
        
        for role_id in roles_to_restore:
            role = member.guild.get_role(role_id)
            if role:
                # 检查用户是否已有该身份组
                if role not in member.roles:
                    roles_to_add.append(role)
                    restored_roles.append(role_id)
                else:
                    # 用户已有该身份组，也算成功
                    restored_roles.append(role_id)
            else:
                # 身份组不存在
                failed_roles.append(role_id)
        
        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="快速处罚撤销")
            except Exception as e:
                print(f"恢复身份组时出错: {e}")
                # 如果添加失败，将这些角色移到失败列表
                for role in roles_to_add:
                    restored_roles.remove(role.id)
                    failed_roles.append(role.id)
        
        return restored_roles, failed_roles
    
    async def send_revoke_log_embed(self, channel: discord.TextChannel,
                                   record: Dict, revoker: discord.User,
                                   restored_roles: List[int], failed_roles: List[int]):
        """发送撤销日志Embed到指定频道"""
        embed = discord.Embed(
            title="↩️ 快速处罚撤销",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name="撤销对象",
            value=f"{record['user_name']} (ID: {record['user_id']})",
            inline=False
        )
        embed.add_field(name="撤销者", value=f"{revoker.mention}", inline=True)
        embed.add_field(name="原执行者", value=record['executor_name'], inline=True)
        
        # 原处罚信息
        embed.add_field(name="原处罚原因", value=record['reason'], inline=False)
        embed.add_field(name="原处罚时间", value=record['timestamp'], inline=False)
        
        # 身份组恢复情况
        if restored_roles:
            roles_str = ", ".join([f"<@&{role_id}>" for role_id in restored_roles])
            embed.add_field(name="✅ 已恢复身份组", value=roles_str, inline=False)
        
        if failed_roles:
            failed_str = ", ".join([f"ID:{role_id}" for role_id in failed_roles])
            embed.add_field(name="❌ 恢复失败的身份组", value=failed_str, inline=False)
        
        embed.set_footer(text=f"撤销的记录ID: {record['id']}")
        
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"发送撤销日志Embed时出错: {e}")
    
    @app_commands.command(name="快速处罚-查询", description="查询最近的快速处罚记录")
    @app_commands.describe(count="要查询的记录数量（默认3条，最多1000条）")
    @app_commands.guild_only()
    async def quick_punish_query(self, interaction: discord.Interaction, count: Optional[int] = 3):
        """查询快速处罚记录命令"""
        # 立即defer响应
        await interaction.response.defer(ephemeral=True)
        
        # 检查功能是否启用
        if not self.enabled:
            await interaction.followup.send(
                "❌ 快速处罚功能未启用",
                ephemeral=True
            )
            return
        
        # 检查权限
        if not self.has_permission(interaction):
            await interaction.followup.send(
                "❌ 您没有权限使用此命令",
                ephemeral=True
            )
            return
        
        # 处理默认值和范围限制
        if count is None:
            count = 3
        count = min(count, 1000)
        count = max(count, 1)
        
        # 获取记录
        records = await self.get_recent_punishments(count)
        
        if not records:
            await interaction.followup.send(
                "📝 暂无快速处罚记录",
                ephemeral=True
            )
            return
        
        # 格式化记录
        formatted_text = await self.format_punishment_records(records, interaction.guild)
        
        # 根据记录数量决定发送方式
        if len(records) <= 10:
            # 10条以内，使用Embed显示
            embed = discord.Embed(
                title=f"📋 最近 {len(records)} 条快速处罚记录",
                description="",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            for i, record in enumerate(records, 1):
                # 解析时间
                try:
                    timestamp = datetime.fromisoformat(record['timestamp'])
                    time_str = timestamp.strftime('%m-%d %H:%M')
                except:
                    time_str = record['timestamp'][:16]
                
                # 状态标记
                status_emoji = {
                    'executed': '✅',
                    'failed': '❌',
                    'revoked': '↩️'
                }.get(record['status'], '❓')
                
                field_name = f"{status_emoji} #{record['id']} - {record['user_name']}"
                field_value = (
                    f"时间: {time_str}\n"
                    f"原因: {record['reason'][:50]}{'...' if len(record['reason']) > 50 else ''}\n"
                    f"执行者: {record['executor_name']}"
                )
                
                embed.add_field(name=field_name, value=field_value, inline=False)
            
            embed.set_footer(text=f"查询者: {interaction.user.name}")
            
            await interaction.followup.send(
                embed=embed,
                ephemeral=True
            )
        else:
            # 超过10条，生成txt文件
            file_content = formatted_text.encode('utf-8')
            file = discord.File(
                io.BytesIO(file_content),
                filename=f"punish_records_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            )
            
            await interaction.followup.send(
                f"📋 找到 {len(records)} 条快速处罚记录，已生成文件：",
                file=file,
                ephemeral=True
            )
    
    @app_commands.command(name="快速处罚-撤销", description="撤销最近一次的快速处罚")
    @app_commands.describe(user_id="要撤销处罚的用户ID")
    @app_commands.guild_only()
    async def quick_punish_revoke(self, interaction: discord.Interaction, user_id: str):
        """撤销快速处罚命令"""
        # 立即defer响应
        await interaction.response.defer(ephemeral=True)
        
        # 检查功能是否启用
        if not self.enabled:
            await interaction.followup.send(
                "❌ 快速处罚功能未启用",
                ephemeral=True
            )
            return
        
        # 检查权限
        if not self.has_permission(interaction):
            await interaction.followup.send(
                "❌ 您没有权限使用此命令",
                ephemeral=True
            )
            return
        
        # 验证用户ID格式
        try:
            user_id = user_id.strip()
            # 尝试转换为整数以验证格式
            int(user_id)
        except ValueError:
            await interaction.followup.send(
                "❌ 无效的用户ID格式，请输入纯数字ID",
                ephemeral=True
            )
            return
        
        # 获取最近的处罚记录
        record = await self.get_last_punishment_for_user(user_id)
        
        if not record:
            await interaction.followup.send(
                f"❌ 未找到用户 {user_id} 的处罚记录",
                ephemeral=True
            )
            return
        
        # 检查记录状态
        if record['status'] == 'revoked':
            await interaction.followup.send(
                f"❌ 该处罚记录已经被撤销过了\n记录ID: {record['id']}",
                ephemeral=True
            )
            return
        
        # 获取用户对象
        guild = interaction.guild
        member = guild.get_member(int(user_id))
        
        restored_roles = []
        failed_roles = []
        
        # 如果用户在服务器中，尝试恢复身份组
        if member and record['removed_roles']:
            restored_roles, failed_roles = await self.restore_user_roles(member, record['removed_roles'])
        elif not member:
            # 用户不在服务器中，无法恢复身份组
            failed_roles = record['removed_roles']
        
        # 更新数据库状态
        success = await self.revoke_punishment(record['id'])
        
        if not success:
            await interaction.followup.send(
                f"❌ 撤销处罚失败，可能记录已被修改",
                ephemeral=True
            )
            return
        
        # 构建成功消息
        success_msg = f"✅ 成功撤销对用户 **{record['user_name']}** (ID: {user_id}) 的处罚\n"
        success_msg += f"记录ID: {record['id']}\n"
        success_msg += f"原处罚时间: {record['timestamp']}\n"
        success_msg += f"原处罚原因: {record['reason']}\n"
        
        if member:
            if restored_roles:
                success_msg += f"✅ 已恢复 {len(restored_roles)} 个身份组\n"
            if failed_roles:
                success_msg += f"⚠️ {len(failed_roles)} 个身份组恢复失败（可能已删除）\n"
        else:
            success_msg += "⚠️ 用户不在服务器中，无法恢复身份组\n"
        
        await interaction.followup.send(success_msg, ephemeral=True)
        
        # 发送到日志频道
        if self.log_channel_id:
            log_channel = guild.get_channel(self.log_channel_id)
            if log_channel:
                await self.send_revoke_log_embed(
                    channel=log_channel,
                    record=record,
                    revoker=interaction.user,
                    restored_roles=restored_roles,
                    failed_roles=failed_roles
                )


# 定义上下文菜单命令（必须在类外部）
@app_commands.context_menu(name="愉悦送走")
@app_commands.guild_only()
async def quick_punish_context(interaction: discord.Interaction, message: discord.Message):
    """快速处罚上下文菜单命令"""
    # 获取cog实例
    cog = interaction.client.get_cog('QuickPunishCog')
    if not cog:
        await interaction.response.send_message(
            "❌ 模块未加载",
            ephemeral=True
        )
        return
    
    # 检查功能是否启用
    if not cog.enabled:
        await interaction.response.send_message(
            "❌ 愉悦送走功能未启用，请联系机器人开发者",
            ephemeral=True
        )
        return
    
    # 检查权限
    if not cog.has_permission(interaction):
        await interaction.response.send_message(
            "❌ 没权。只有管理组和类脑自研答疑AI可以给人愉悦送走。",
            ephemeral=True
        )
        return
    
    # 检查目标是否是机器人
    if message.author.bot:
        await interaction.response.send_message(
            "❌ 不能给Bot愉悦送走。",
            ephemeral=True
        )
        return
    
    # 显示确认表单
    modal = QuickPunishModal(target_message=message, cog=cog)
    await interaction.response.send_modal(modal)


async def setup(bot):
    """设置Cog"""
    # 添加Cog
    await bot.add_cog(QuickPunishCog(bot))
    
    # 添加上下文菜单命令
    bot.tree.add_command(quick_punish_context)