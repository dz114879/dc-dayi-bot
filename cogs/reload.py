import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import os
from datetime import datetime

def is_admin(interaction: discord.Interaction) -> bool:
    """检查用户是否为机器人的管理员"""
    return interaction.user.id in interaction.client.admins

class ReloadCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _log_slash_command(self, interaction: discord.Interaction, success: bool):
        """记录斜杠命令的使用情况"""
        log_dir = 'logs'
        log_file = os.path.join(log_dir, 'log.txt')

        if not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir)
            except OSError as e:
                print(f" [31m[错误] [0m 创建日志文件夹 {log_dir} 失败: {e}")
                return

        try:
            user_id = interaction.user.id
            user_name = interaction.user.name
            command_name = interaction.command.name if interaction.command else "Unknown"
            status = "成功" if success else "失败"
            
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_entry = f"[{timestamp}] ({user_id}+{user_name}+/{command_name}+{status})\n"

            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)
        except Exception as e:
            print(f" [31m[错误] [0m 写入日志文件失败: {e}")

    def _load_database(self):
        """从 users.db SQLite数据库加载数据到 bot 实例"""
        try:
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            
            # 加载管理员
            cursor.execute("SELECT id FROM admins")
            self.bot.admins = [int(row[0]) for row in cursor.fetchall()]
            
            # 加载受信任用户
            cursor.execute("SELECT id FROM trusted_users")
            self.bot.trusted_users = [int(row[0]) for row in cursor.fetchall()]
            
            # 加载用户数据
            cursor.execute("SELECT id, quota, time FROM users")
            self.bot.users_data = []
            for row in cursor.fetchall():
                user_data = {
                    'id': row[0],
                    'quota': row[1],
                    'time': row[2],
                    'banned': False  # 默认值，因为数据库中没有banned字段
                }
                self.bot.users_data.append(user_data)
            
            self.bot.registered_users = [int(user['id']) for user in self.bot.users_data]
            
            conn.close()
        except sqlite3.Error as e:
            print(f" [31m[错误] [0m SQLite数据库错误: {e}。将使用空数据库。")
            self.bot.admins = []
            self.bot.trusted_users = []
            self.bot.users_data = []
            self.bot.registered_users = []
        except Exception as e:
            print(f" [31m[错误] [0m 加载数据库时发生未知错误: {e}。将使用空数据库。")
            self.bot.admins = []
            self.bot.trusted_users = []
            self.bot.users_data = []
            self.bot.registered_users = []

    @app_commands.command(name='reload-db', description='[仅管理员] 重新加载数据库文件 users.db')
    @app_commands.check(is_admin)
    async def reload_db(self, interaction: discord.Interaction):
        """重新加载SQLite数据库文件"""
        try:
            self._load_database()
            await interaction.response.send_message('✅ 数据库 `users.db` 已成功重新加载。', ephemeral=True)
            self._log_slash_command(interaction, True)
            print(f"👑 数据库已由管理员 {interaction.user.name} ({interaction.user.id}) 手动重新加载。")
            print(f'👑 新的管理员ID: {self.bot.admins}')
            print(f'🤝 新的受信任用户ID: {self.bot.trusted_users}')
            print(f'👥 用户数据库已重新加载，包含 {len(self.bot.users_data)} 个用户条目。')
        except Exception as e:
            await interaction.response.send_message(f'❌ 重新加载数据库时发生错误: {e}', ephemeral=True)
            print(f" [31m[错误] [0m 手动重新加载数据库失败: {e}")
            self._log_slash_command(interaction, False)
            
    @reload_db.error
    async def on_reload_db_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """处理 reload_db 命令的特定错误"""
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message('❌ 你没有权限使用此命令。', ephemeral=True)
        else:
            print(f' 未处理的斜杠命令错误 in ReloadCog: {error}')
            await interaction.response.send_message('❌ 执行命令时发生未知错误。', ephemeral=True)
        # 在任何错误情况下都记录失败
        self._log_slash_command(interaction, False)

async def setup(bot: commands.Bot):
    await bot.add_cog(ReloadCog(bot))