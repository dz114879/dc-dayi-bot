import discord
from discord.ext import commands
from discord import app_commands
import os
import openai
import asyncio
import mimetypes
import base64
from datetime import datetime, timedelta
import json
import random
from typing import Optional, List, Dict, Tuple
from cogs.rag_processor import RAGProcessor
from PIL import Image
import io
import re

# --- 安全的 defer 函数 ---
async def safe_defer(interaction: discord.Interaction):
    """
    一个绝对安全的"占坑"函数。
    它会检查交互是否已被响应，如果没有，就立即以"仅自己可见"的方式延迟响应，
    这能完美解决超时和重复响应问题。
    """
    if not interaction.response.is_done():
        # ephemeral=True 让这个"占坑"行为对其他人不可见，不刷屏。
        await interaction.response.defer(ephemeral=True)

class Summary(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 默认的提示词文件路径
        self.prompt_head_path = "rag_prompt/summary_head.txt"
        self.prompt_end_path = "rag_prompt/summary_end.txt"
        
    def parse_discord_link(self, link: str) -> Tuple[int, int, int]:
        """
        解析Discord消息链接，提取guild_id, channel_id, message_id
        
        Args:
            link: Discord消息链接
            
        Returns:
            (guild_id, channel_id, message_id)
            
        Raises:
            ValueError: 如果链接格式无效
        """
        # Discord消息链接格式: https://discord.com/channels/guild_id/channel_id/message_id
        pattern = r'https://discord\.com/channels/(\d+)/(\d+)/(\d+)'
        match = re.match(pattern, link.strip())
        
        if not match:
            # 尝试其他可能的格式
            pattern2 = r'https://discordapp\.com/channels/(\d+)/(\d+)/(\d+)'
            match = re.match(pattern2, link.strip())
            
        if not match:
            raise ValueError("无效的Discord消息链接格式")
            
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    
    async def fetch_messages_batch(self, channel: discord.TextChannel,
                                  start_message: discord.Message,
                                  count: int) -> List[discord.Message]:
        """
        分批获取消息，每100条休息2秒
        
        Args:
            channel: 目标频道
            start_message: 起始消息
            count: 要获取的消息数量
            
        Returns:
            消息列表（按时间倒序，即最新的在前）
        """
        messages = [start_message]  # 🔥 关键修复：包含起始消息
        remaining = count - 1  # 已经包含了起始消息，所以减1
        before = start_message
        
        # 添加调试日志
        print(f"📍 开始获取消息，起始消息ID: {start_message.id}")
        print(f"📍 需要获取总数: {count} 条（包含起始消息）")
        
        while remaining > 0:
            batch_size = min(100, remaining)
            
            try:
                # 获取一批消息
                batch = []
                async for msg in channel.history(limit=batch_size, before=before):
                    batch.append(msg)
                
                if not batch:
                    print(f"📍 没有更多消息了，已获取 {len(messages)} 条")
                    break
                
                messages.extend(batch)
                remaining -= len(batch)
                before = batch[-1]  # 更新before为这批最后一条消息
                
                # 如果还有更多消息要获取，休息2秒
                if remaining > 0:
                    print(f"📥 已获取 {len(messages)} 条消息，休息2秒...")
                    await asyncio.sleep(2)
                    
            except discord.Forbidden:
                print(f"❌ 无权限获取频道 {channel.name} 的消息")
                break
            except discord.HTTPException as e:
                print(f"❌ 获取消息时发生HTTP错误: {e}")
                break
                
        return messages
    
    def format_messages_for_prompt(self, messages: List[discord.Message]) -> str:
        """
        格式化消息列表为提示词格式
        
        Args:
            messages: 消息列表
            
        Returns:
            格式化后的消息文本
        """
        formatted_lines = []
        
        # 消息是倒序的（最新的在前），我们需要反转以获得正确的时间顺序
        messages_reversed = list(reversed(messages))
        
        for idx, msg in enumerate(messages_reversed):
            # 每50条消息记录一次时间戳（第1条、第51条、第101条...）
            if idx == 0 or idx % 50 == 0:
                timestamp = msg.created_at.strftime('%Y-%m-%d %H:%M:%S')
                formatted_lines.append(f"\n--- 时间戳: {timestamp} ---\n")
            
            # 格式化消息内容
            author_name = msg.author.display_name
            content = msg.content if msg.content else "[无文本内容]"
            
            # 如果消息有附件，添加附件说明
            if msg.attachments:
                attachments_info = f" [附件: {', '.join([att.filename for att in msg.attachments])}]"
                content += attachments_info
            
            # 如果消息有嵌入（embed），添加说明
            if msg.embeds:
                content += f" [包含{len(msg.embeds)}个嵌入内容]"
            
            formatted_lines.append(f"[{author_name}]: {content}")
        
        return "\n".join(formatted_lines)
    
    def load_prompts(self) -> Tuple[str, str]:
        """
        加载提示词头部和尾部
        
        Returns:
            (head_prompt, end_prompt)
        """
        try:
            with open(self.prompt_head_path, 'r', encoding='utf-8') as f:
                head_prompt = f.read().strip()
        except FileNotFoundError:
            print(f"⚠️ 未找到 {self.prompt_head_path}，使用默认头部提示词")
            head_prompt = "请总结以下Discord消息记录：\n"
        
        try:
            with open(self.prompt_end_path, 'r', encoding='utf-8') as f:
                end_prompt = f.read().strip()
        except FileNotFoundError:
            print(f"⚠️ 未找到 {self.prompt_end_path}，使用默认尾部提示词")
            end_prompt = "\n请提供详细的总结和分析。"
        
        return head_prompt, end_prompt
    
    @app_commands.command(name="大法官开庭", description="对Discord消息进行AI总结和评判")
    @app_commands.describe(
        message_link="Discord消息链接（右键消息->复制消息链接）",
        message_count="要分析的消息数量（最多500条）"
    )
    async def summarize_messages(self, 
                                interaction: discord.Interaction, 
                                message_link: str,
                                message_count: int):
        """
        AI快速总结并评判功能的斜杠命令
        """
        # 🔥 黄金法则：永远先 defer！
        await safe_defer(interaction)
        
        # 权限检查
        user_id = interaction.user.id
        if not (user_id in self.bot.admins or user_id in self.bot.trusted_users):
            await interaction.edit_original_response(
                content='❌ 没有权限。此命令仅限答疑组使用。'
            )
            return
        
        # 参数验证
        if message_count < 1:
            await interaction.edit_original_response(
                content='❌ 消息数量必须至少为1条。'
            )
            return
        
        if message_count > 500:
            await interaction.edit_original_response(
                content='❌ 消息数量不能超过500条。'
            )
            return
        
        # 解析消息链接
        try:
            guild_id, channel_id, message_id = self.parse_discord_link(message_link)
        except ValueError as e:
            await interaction.edit_original_response(
                content=f'❌ {str(e)}\n'
                       f'正确格式: https://discord.com/channels/服务器ID/频道ID/消息ID'
            )
            return
        
        # 检查是否在同一个服务器
        if interaction.guild_id != guild_id:
            await interaction.edit_original_response(
                content='❌ 只能总结当前服务器的消息。'
            )
            return
        
        # 获取频道
        channel = interaction.guild.get_channel(channel_id)
        if not channel:
            await interaction.edit_original_response(
                content='❌ 找不到指定的频道。'
            )
            return
        
        # 检查用户是否有权限查看该频道
        if not channel.permissions_for(interaction.user).read_messages:
            await interaction.edit_original_response(
                content='❌ 你没有权限查看该频道的消息。'
            )
            return
        
        # 检查机器人是否有权限读取该频道的历史消息
        if not channel.permissions_for(interaction.guild.me).read_message_history:
            await interaction.edit_original_response(
                content='❌ 机器人没有权限读取该频道的历史消息。'
            )
            return
        
        # 获取起始消息
        try:
            start_message = await channel.fetch_message(message_id)
        except discord.NotFound:
            await interaction.edit_original_response(
                content='❌ 找不到指定的消息，可能已被删除。'
            )
            return
        except discord.Forbidden:
            await interaction.edit_original_response(
                content='❌ 没有权限获取该消息。'
            )
            return
        
        # 更新状态
        await interaction.edit_original_response(
            content=f'⏳ 正在获取 {message_count} 条消息...\n'
                   f'起始消息: {start_message.author.display_name} - {start_message.created_at.strftime("%Y-%m-%d %H:%M")}'
        )
        
        # 获取消息
        try:
            messages = await self.fetch_messages_batch(channel, start_message, message_count)
            
            if not messages:
                await interaction.edit_original_response(
                    content='❌ 未能获取到任何消息。'
                )
                return
            
            actual_count = len(messages)
            
            # 计算时间跨度
            if messages:
                newest_time = start_message.created_at
                oldest_time = messages[-1].created_at
                time_span = newest_time - oldest_time
                
                # 格式化时间跨度
                days = time_span.days
                hours = time_span.seconds // 3600
                minutes = (time_span.seconds % 3600) // 60
                
                if days > 0:
                    time_span_str = f"{days}天{hours}小时{minutes}分钟"
                elif hours > 0:
                    time_span_str = f"{hours}小时{minutes}分钟"
                else:
                    time_span_str = f"{minutes}分钟"
            else:
                time_span_str = "未知"
            
            # 统计参与者
            participants = set()
            for msg in messages:
                participants.add(msg.author.display_name)
            
            await interaction.edit_original_response(
                content=f'📊 已获取 {actual_count} 条消息\n'
                       f'⏱️ 时间跨度: {time_span_str}\n'
                       f'👥 参与者: {len(participants)} 人\n'
                       f'⏳ 正在进行AI分析...'
            )
            
        except Exception as e:
            await interaction.edit_original_response(
                content=f'❌ 获取消息时出错: {str(e)}'
            )
            return
        
        # 格式化消息
        formatted_messages = self.format_messages_for_prompt(messages)
        
        # 加载提示词
        head_prompt, end_prompt = self.load_prompts()
        
        # 构建完整的提示词
        full_prompt = f"{head_prompt}\n{formatted_messages}\n{end_prompt}"
        
        # 添加调试日志
        print(f"📊 准备发送给AI的消息统计:")
        print(f"  - 实际消息数: {len(messages)} 条")
        print(f"  - 格式化后文本长度: {len(formatted_messages)} 字符")
        print(f"  - 完整提示词长度: {len(full_prompt)} 字符")
        
        # 调用OpenAI API
        try:
            if not hasattr(self.bot, 'openai_client') or not self.bot.openai_client:
                await interaction.edit_original_response(
                    content='❌ OpenAI客户端未初始化。'
                )
                return
            
            # 构建消息
            messages_for_api = [
                {"role": "system", "content": "你是一个专业的对话分析助手，擅长总结和评判讨论内容。"},
                {"role": "user", "content": full_prompt}
            ]
            
            # 异步调用API（设置2分钟超时）
            loop = asyncio.get_event_loop()
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self.bot.openai_client.chat.completions.create(
                        model="gemini-2.5-pro-preview-06-05",  # 🔥 硬编码模型
                        messages=messages_for_api,
                        temperature=1.0,
                        max_tokens=8192
                    )
                ),
                timeout=180.0  # 3分钟超时
            )
            
            if not response or not response.choices:
                await interaction.edit_original_response(
                    content='❌ AI返回了空响应。'
                )
                return
            
            ai_response = response.choices[0].message.content
            
        except asyncio.TimeoutError:
            await interaction.edit_original_response(
                content='⏱️ AI分析超时（超过2分钟），请减少消息数量后重试。'
            )
            return
        except Exception as e:
            await interaction.edit_original_response(
                content=f'❌ AI分析时出错: {str(e)}'
            )
            return
        
        # 创建embed回复
        embed = discord.Embed(
            title="📝 消息总结与评判",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        # 添加统计信息
        embed.add_field(
            name="📊 统计信息",
            value=f"**消息数量**: {actual_count} 条\n"
                  f"**时间跨度**: {time_span_str}\n"
                  f"**参与人数**: {len(participants)} 人\n"
                  f"**频道**: <#{channel_id}>",
            inline=False
        )
        
        # 将AI响应分段添加到embed（Discord embed描述有字符限制）
        if len(ai_response) <= 4000:
            embed.description = ai_response
        else:
            # 如果内容太长，截断并提示
            embed.description = ai_response[:4000] + "\n\n...[内容过长，已截断]"
        
        # 设置页脚
        embed.set_footer(
            text=f"分析者: {interaction.user.display_name} | 模型: gemini-2.5-pro-preview-06-05"
        )
        
        # 发送到频道（公开）
        await interaction.channel.send(embed=embed)
        
        # 更新原始响应（私有）
        await interaction.edit_original_response(
            content='✅ 总结已完成并发送到频道。'
        )
        
        print(f"✅ 用户 {interaction.user.id} 成功总结了 {actual_count} 条消息")
        print(f"📊 最终统计: 获取 {len(messages)} 条，格式化 {len(formatted_messages)} 字符")

async def setup(bot: commands.Bot):
    """设置Cog"""
    # 确保OpenAI客户端已初始化
    if not hasattr(bot, 'openai_client'):
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        OPENAI_API_BASE_URL = os.getenv("OPENAI_API_BASE_URL")
        
        if not all([OPENAI_API_KEY, OPENAI_API_BASE_URL]):
            print("❌ [Summary] 缺少必要的OpenAI环境变量")
            bot.openai_client = None
        else:
            bot.openai_client = openai.OpenAI(
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_API_BASE_URL,
            )
            print("✅ [Summary] OpenAI客户端已初始化")
    
    await bot.add_cog(Summary(bot))
    print("✅ Summary Cog 已加载")