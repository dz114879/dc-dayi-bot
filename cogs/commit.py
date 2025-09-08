import discord
from discord.ext import commands
from discord import app_commands
import os
import openai
import asyncio
import sqlite3
import re
from datetime import datetime
from cogs.logger import log_slash_command
import aiofiles
import traceback

# 从register.py导入safe_defer函数
async def safe_defer(interaction: discord.Interaction):
    """
    一个绝对安全的"占坑"函数。
    它会检查交互是否已被响应，如果没有，就立即以"仅自己可见"的方式延迟响应，
    这能完美解决超时和重复响应问题。
    """
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)


class CommitCog(commands.Cog):
    """用户反馈功能Cog"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_name = 'feedback.db'
        self.init_database()
        
    def init_database(self):
        """初始化反馈记录数据库"""
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            
            # 创建反馈记录表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS feedback_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feedback_id TEXT UNIQUE NOT NULL,
                    user_id TEXT NOT NULL,
                    message_link TEXT NOT NULL,
                    original_content TEXT,
                    correction TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    ai_response TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 创建每日计数表（用于生成唯一的反馈编号）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS daily_counter (
                    date TEXT PRIMARY KEY,
                    count INTEGER DEFAULT 0
                )
            ''')
            
            conn.commit()
            conn.close()
            print("✅ 反馈数据库初始化成功")
        except sqlite3.Error as e:
            print(f"❌ 初始化反馈数据库时出错: {e}")
    
    def parse_discord_link(self, link: str):
        """
        解析Discord消息链接
        支持格式：
        - https://discord.com/channels/服务器ID/频道ID/消息ID
        - https://canary.discord.com/channels/服务器ID/频道ID/消息ID
        - https://ptb.discord.com/channels/服务器ID/频道ID/消息ID
        """
        pattern = r'https?://(?:canary\.|ptb\.)?discord\.com/channels/(\d+)/(\d+)/(\d+)'
        match = re.match(pattern, link)
        
        if match:
            guild_id = int(match.group(1))
            channel_id = int(match.group(2))
            message_id = int(match.group(3))
            return guild_id, channel_id, message_id
        return None, None, None
    
    def generate_feedback_id(self):
        """生成唯一的反馈编号，格式：FB-YYYYMMDD-XXXX"""
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            
            today = datetime.now().strftime('%Y%m%d')
            
            # 获取今日计数
            cursor.execute('SELECT count FROM daily_counter WHERE date = ?', (today,))
            result = cursor.fetchone()
            
            if result:
                count = result[0] + 1
                cursor.execute('UPDATE daily_counter SET count = ? WHERE date = ?', (count, today))
            else:
                count = 1
                cursor.execute('INSERT INTO daily_counter (date, count) VALUES (?, ?)', (today, 1))
            
            conn.commit()
            conn.close()
            
            # 生成反馈编号
            feedback_id = f"FB-{today}-{count:04d}"
            return feedback_id
            
        except sqlite3.Error as e:
            print(f"❌ 生成反馈编号时出错: {e}")
            # 如果数据库出错，使用时间戳作为备选方案
            timestamp = int(datetime.now().timestamp())
            return f"FB-{timestamp}"
    
    async def load_prompt_files(self):
        """加载提示词文件"""
        prompt_head = ""
        prompt_end = ""
        
        try:
            # 尝试读取commit_head.txt
            if os.path.exists('commit_prompt/commit_head.txt'):
                async with aiofiles.open('commit_prompt/commit_head.txt', 'r', encoding='utf-8') as f:
                    prompt_head = await f.read()
                    prompt_head = prompt_head.strip()
            
            # 尝试读取commit_end.txt
            if os.path.exists('commit_prompt/commit_end.txt'):
                async with aiofiles.open('commit_prompt/commit_end.txt', 'r', encoding='utf-8') as f:
                    prompt_end = await f.read()
                    prompt_end = prompt_end.strip()
                    
        except Exception as e:
            print(f"⚠️ 读取提示词文件时出错: {e}")
        
        return prompt_head, prompt_end
    
    async def append_to_commited(self, content: str):
        """追加内容到commited.txt文件"""
        try:
            # 确保目录存在
            os.makedirs('rag_prompt', exist_ok=True)
            
            # 追加内容到文件
            async with aiofiles.open('rag_prompt/commited.txt', 'a', encoding='utf-8') as f:
                await f.write('\n' + content + '\n')
            
            return True
        except Exception as e:
            print(f"❌ 追加内容到commited.txt时出错: {e}")
            return False
    
    def format_message_content(self, message: discord.Message) -> str:
        """
        格式化消息内容，包括文本、Embeds 和附件
        返回格式化后的字符串
        """
        content_parts = []
        
        # 1. 文本内容
        if message.content:
            content_parts.append(f"【文本内容】\n{message.content}")
        
        # 2. Embeds 内容
        if message.embeds:
            embed_parts = []
            for i, embed in enumerate(message.embeds):
                embed_text = f"【Embed {i+1}】"
                embed_fields = []
                
                if embed.title:
                    embed_fields.append(f"标题: {embed.title}")
                if embed.description:
                    embed_fields.append(f"描述: {embed.description}")
                if embed.author and embed.author.name:
                    embed_fields.append(f"作者: {embed.author.name}")
                
                # 处理字段
                if embed.fields:
                    field_texts = []
                    for field in embed.fields:
                        field_text = f"{field.name}: {field.value}"
                        field_texts.append(field_text)
                    if field_texts:
                        embed_fields.append("字段:\n  " + "\n  ".join(field_texts))
                
                if embed.footer and embed.footer.text:
                    embed_fields.append(f"页脚: {embed.footer.text}")
                
                if embed_fields:
                    embed_text += "\n" + "\n".join(embed_fields)
                embed_parts.append(embed_text)
            
            if embed_parts:
                content_parts.append("\n".join(embed_parts))
        
        # 3. 附件内容
        if message.attachments:
            attachment_parts = ["【附件】"]
            for i, attachment in enumerate(message.attachments):
                att_info = f"{i+1}. {attachment.filename}"
                
                # 判断文件类型
                if attachment.content_type:
                    if attachment.content_type.startswith('image/'):
                        att_info += f" (图片)"
                    elif attachment.content_type.startswith('video/'):
                        att_info += f" (视频)"
                    elif attachment.content_type.startswith('audio/'):
                        att_info += f" (音频)"
                    else:
                        att_info += f" ({attachment.content_type})"
                
                # 添加文件大小信息
                if attachment.size:
                    size_mb = attachment.size / (1024 * 1024)
                    if size_mb < 1:
                        att_info += f" [{attachment.size / 1024:.1f} KB]"
                    else:
                        att_info += f" [{size_mb:.1f} MB]"
                
                att_info += f"\n   链接: {attachment.url}"
                attachment_parts.append(att_info)
            
            content_parts.append("\n".join(attachment_parts))
        
        # 如果没有任何内容
        if not content_parts:
            return "[消息无内容]"
        
        return "\n\n".join(content_parts)
    
    def is_registered(self, user_id: int) -> bool:
        """检查用户是否已注册"""
        return user_id in self.bot.registered_users
    
    @app_commands.command(name='反馈', description='提交对AI回复的改正反馈')
    @app_commands.describe(
        message_link='Discord消息链接',
        correction='改正内容（纯文本）',
        reason='改正理由（纯文本）'
    )
    async def feedback(self, interaction: discord.Interaction, 
                      message_link: str, 
                      correction: str, 
                      reason: str):
        """反馈命令主函数"""
        await safe_defer(interaction)
        
        # 权限检查 - 仅已注册用户可用
        if not self.is_registered(interaction.user.id):
            await interaction.edit_original_response(
                content='❌ 此命令仅限已注册用户使用。请先使用 `/register` 命令注册。'
            )
            log_slash_command(interaction, False)
            return
        
        try:
            # 生成反馈编号
            feedback_id = self.generate_feedback_id()
            
            # 解析Discord消息链接
            guild_id, channel_id, message_id = self.parse_discord_link(message_link)
            
            if not all([guild_id, channel_id, message_id]):
                await interaction.edit_original_response(
                    content='❌ 无效的Discord消息链接。请确保链接格式正确。'
                )
                log_slash_command(interaction, False)
                return
            
            # 获取原始消息内容
            original_content = None
            message_author = None
            
            try:
                # 尝试获取频道和消息
                channel = self.bot.get_channel(channel_id)
                if channel:
                    message = await channel.fetch_message(message_id)
                    message_author = f"{message.author.name}#{message.author.discriminator}"
                    
                    # 使用新的格式化方法
                    original_content = self.format_message_content(message)
                    
                else:
                    # 如果无法获取频道，可能是机器人没有权限
                    original_content = "[无法获取消息内容：机器人可能没有访问权限]"
                    message_author = "[未知]"
            except discord.Forbidden:
                original_content = "[无法获取消息内容：权限不足]"
                message_author = "[未知]"
            except discord.NotFound:
                original_content = "[无法获取消息内容：消息不存在]"
                message_author = "[未知]"
            except Exception as e:
                original_content = f"[获取消息时出错：{str(e)}]"
                message_author = "[未知]"
            
            # 获取反馈频道
            commit_channel_id = os.getenv('COMMIT_CHANNEL_ID')
            if not commit_channel_id:
                await interaction.edit_original_response(
                    content='❌ 系统配置错误：未设置反馈频道。请联系管理员。'
                )
                log_slash_command(interaction, False)
                return
            
            commit_channel = self.bot.get_channel(int(commit_channel_id))
            if not commit_channel:
                await interaction.edit_original_response(
                    content='❌ 系统配置错误：无法找到反馈频道。请联系管理员。'
                )
                log_slash_command(interaction, False)
                return
            
            # 第一步：转发反馈信息到指定频道
            feedback_embed = discord.Embed(
                title=f"📝 新反馈 - {feedback_id}",
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            feedback_embed.add_field(
                name="提交者",
                value=f"{interaction.user.mention} ({interaction.user.id})",
                inline=True
            )
            feedback_embed.add_field(
                name="消息链接",
                value=f"[点击查看]({message_link})",
                inline=True
            )
            feedback_embed.add_field(
                name="原始作者",
                value=message_author,
                inline=True
            )
            # 对于长内容，进行智能截断并确保不超过 Discord 的字段值限制
            if original_content and len(original_content) > 1024:
                # 尝试在合适的位置截断（如换行符）
                truncate_pos = 1000
                newline_pos = original_content.rfind('\n', 0, truncate_pos)
                if newline_pos > 800:  # 如果找到合适的换行位置
                    display_content = original_content[:newline_pos] + "\n... (内容已截断)"
                else:
                    display_content = original_content[:truncate_pos] + "... (内容已截断)"
            else:
                display_content = original_content
            
            feedback_embed.add_field(
                name="原始内容",
                value=display_content if display_content else "[无内容]",
                inline=False
            )
            feedback_embed.add_field(
                name="改正内容",
                value=correction[:1024],
                inline=False
            )
            feedback_embed.add_field(
                name="改正理由",
                value=reason[:1024],
                inline=False
            )
            
            await commit_channel.send(embed=feedback_embed)
            
            # 第二步：构建AI提示词
            prompt_head, prompt_end = await self.load_prompt_files()
            
            # 构建完整提示词
            full_prompt = f"{prompt_head}\n" if prompt_head else ""
            full_prompt += f"原始消息内容：\n{original_content}\n\n" if original_content else ""
            full_prompt += f"用户提供的改正内容：{correction}\n\n"
            full_prompt += f"改正理由：{reason}\n"
            full_prompt += f"{prompt_end}" if prompt_end else ""
            
            # 第三步：调用OpenAI API
            ai_response = None
            try:
                # 检查是否有并发限制
                if not hasattr(self.bot, 'current_parallel_commit_tasks'):
                    self.bot.current_parallel_commit_tasks = 0
                
                max_parallel = int(os.getenv("MAX_PARALLEL", 5))
                if self.bot.current_parallel_commit_tasks >= max_parallel:
                    await interaction.edit_original_response(
                        content=f"⚠️ 当前处理队列已满，但您的反馈已记录（编号：{feedback_id}）。AI处理将稍后进行。"
                    )
                    # 保存到数据库但不处理AI
                    self.save_feedback_record(
                        feedback_id, str(interaction.user.id), message_link,
                        original_content, correction, reason, "[等待处理]"
                    )
                    log_slash_command(interaction, True)
                    return
                
                self.bot.current_parallel_commit_tasks += 1
                
                # 调用API
                client = self.bot.openai_client
                messages = [
                    {"role": "user", "content": full_prompt}
                ]
                
                # 使用asyncio执行API调用，设置3分钟超时
                loop = asyncio.get_event_loop()
                response = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda: client.chat.completions.create(
                            model=os.getenv("OPENAI_MODEL"),
                            messages=messages,
                            temperature=1,
                            stream=False
                        )
                    ),
                    timeout=180.0  # 3分钟超时
                )
                
                ai_response = response.choices[0].message.content
                
            except asyncio.TimeoutError:
                ai_response = "[处理超时：AI处理时间超过3分钟]"
            except Exception as e:
                ai_response = f"[AI处理出错：{str(e)}]"
                print(f"❌ 调用OpenAI API时出错: {e}")
                traceback.print_exc()
            finally:
                if hasattr(self.bot, 'current_parallel_commit_tasks'):
                    self.bot.current_parallel_commit_tasks -= 1
            
            # 第四步：将AI响应发送到反馈频道
            if ai_response:
                ai_embed = discord.Embed(
                    title=f"🤖 AI分析结果 - {feedback_id}",
                    description=ai_response[:4096],  # Discord embed描述限制
                    color=discord.Color.green(),
                    timestamp=datetime.utcnow()
                )
                ai_embed.set_footer(text=f"模型：{os.getenv('OPENAI_MODEL')}")
                
                await commit_channel.send(embed=ai_embed)
                
                # 第五步：追加到commited.txt（只保留AI分析的Q&A内容）
                # AI响应已经是Q&A格式，直接追加
                await self.append_to_commited(ai_response)
            
            # 保存到数据库
            self.save_feedback_record(
                feedback_id, str(interaction.user.id), message_link,
                original_content, correction, reason, ai_response
            )
            
            # 第六步：向用户发送感谢消息
            success_embed = discord.Embed(
                title="✅ 感谢您的反馈！",
                description=f"您的反馈已成功提交并处理。\n\n**反馈编号：** `{feedback_id}`",
                color=discord.Color.green()
            )
            success_embed.add_field(
                name="后续处理",
                value="您的反馈将用于改进AI的回复质量。感谢您对社区的贡献！",
                inline=False
            )
            success_embed.set_footer(text="此消息仅您可见")
            
            await interaction.edit_original_response(embed=success_embed)
            log_slash_command(interaction, True)
            
            print(f"✅ 用户 {interaction.user.name} ({interaction.user.id}) 提交了反馈 {feedback_id}")
            
        except Exception as e:
            print(f"❌ 处理反馈时出现未预期的错误: {e}")
            traceback.print_exc()
            
            error_embed = discord.Embed(
                title="❌ 处理失败",
                description="处理您的反馈时出现错误。请稍后再试或联系管理员。",
                color=discord.Color.red()
            )
            error_embed.add_field(
                name="错误信息",
                value=str(e)[:1024],
                inline=False
            )
            
            await interaction.edit_original_response(embed=error_embed)
            log_slash_command(interaction, False)
    
    def save_feedback_record(self, feedback_id, user_id, message_link, 
                           original_content, correction, reason, ai_response):
        """保存反馈记录到数据库"""
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO feedback_records 
                (feedback_id, user_id, message_link, original_content, 
                 correction, reason, ai_response)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (feedback_id, user_id, message_link, original_content,
                  correction, reason, ai_response))
            
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            print(f"❌ 保存反馈记录时出错: {e}")


async def setup(bot: commands.Bot):
    """加载Cog"""
    # 确保bot有openai_client属性
    if not hasattr(bot, 'openai_client'):
        # 从.env文件加载配置
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        OPENAI_API_BASE_URL = os.getenv("OPENAI_API_BASE_URL")
        if not all([OPENAI_API_KEY, OPENAI_API_BASE_URL]):
            print("❌ [错误](来自Commit) 缺少必要的 OpenAI 环境变量。")
            bot.openai_client = None
        else:
            bot.openai_client = openai.OpenAI(
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_API_BASE_URL,
            )
    
    await bot.add_cog(CommitCog(bot))
    print("✅ Commit cog 已加载")