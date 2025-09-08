import discord
from discord.ext import commands
from discord import app_commands
import os
from datetime import datetime

def log_slash_command(interaction: discord.Interaction, success: bool):
    """记录斜杠命令的使用情况"""
    log_dir = 'logs'
    log_file = os.path.join(log_dir, 'log.txt')

    # 确保logs文件夹存在
    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError as e:
            print(f" [31m[错误] [0m 创建日志文件夹 {log_dir} 失败: {e}")
            return

    try:
        user_id = interaction.user.id
        user_name = interaction.user.name
        # 修正：在错误处理中 interaction.command 可能为 None
        command_name = interaction.command.name if interaction.command else "Unknown"
        status = "成功" if success else "失败"
        
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"[{timestamp}] ({user_id}+{user_name}+/{command_name}+{status})\n"

        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    except Exception as e:
        print(f" [31m[错误] [0m 写入日志文件失败: {e}")

class Logger(commands.Cog):
    """日志记录功能的Cog"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.Cog.listener()
    async def on_ready(self):
        print('✅ Logger cog 已加载')

async def setup(bot):
    await bot.add_cog(Logger(bot))
