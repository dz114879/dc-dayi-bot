import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import os
from datetime import datetime

def is_admin(interaction: discord.Interaction) -> bool:
    """检查用户是否为机器人的管理员"""
    return interaction.user.id in interaction.client.admins

class RoleSyncCog(commands.Cog):
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

    def _update_bot_data(self):
        """重新加载机器人内存中的用户数据"""
        try:
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            
            # 重新加载管理员
            cursor.execute("SELECT id FROM admins")
            self.bot.admins = [int(row[0]) for row in cursor.fetchall()]
            
            # 重新加载受信任用户
            cursor.execute("SELECT id FROM trusted_users")
            self.bot.trusted_users = [int(row[0]) for row in cursor.fetchall()]
            
            # 重新加载kn_owner用户组
            try:
                cursor.execute("SELECT id FROM kn_owner")
                self.bot.kn_owner = [int(row[0]) for row in cursor.fetchall()]
            except sqlite3.OperationalError:
                # 如果kn_owner表不存在，初始化为空列表
                self.bot.kn_owner = []
            
            conn.close()
        except sqlite3.Error as e:
            print(f" [31m[错误] [0m 更新机器人数据时出错: {e}")

    @app_commands.command(name='syncrole', description='[仅管理员] 同步Discord身份组到数据库身份组')
    @app_commands.describe(
        discord_role_id='Discord身份组ID（如1354043091757305911）',
        db_group='要同步到的数据库身份组'
    )
    @app_commands.choices(db_group=[
        app_commands.Choice(name="admins", value="admins"),
        app_commands.Choice(name="trusted_users", value="trusted_users"),
        app_commands.Choice(name="kn_owner", value="kn_owner")
    ])
    @app_commands.check(is_admin)
    async def syncrole(self, interaction: discord.Interaction, discord_role_id: str, db_group: str):
        """同步Discord身份组到数据库身份组，只有管理员可以使用"""
        
        # 验证Discord身份组ID格式
        try:
            role_id = int(discord_role_id)
        except ValueError:
            await interaction.response.send_message(f'❌ 无效的Discord身份组ID格式: `{discord_role_id}`。请输入有效的数字ID。', ephemeral=True)
            self._log_slash_command(interaction, False)
            return

        # 获取Discord身份组对象
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message('❌ 此命令只能在服务器中使用。', ephemeral=True)
            self._log_slash_command(interaction, False)
            return

        discord_role = guild.get_role(role_id)
        if not discord_role:
            await interaction.response.send_message(f'❌ 在当前服务器中找不到ID为 `{role_id}` 的身份组。', ephemeral=True)
            self._log_slash_command(interaction, False)
            return

        # 延迟响应，因为扫描可能需要时间
        await interaction.response.defer(ephemeral=True)

        try:
            # 扫描服务器中拥有指定身份组的用户
            users_with_role = []
            for member in guild.members:
                if discord_role in member.roles:
                    users_with_role.append(member.id)

            if not users_with_role:
                await interaction.followup.send(f'ℹ️ 在服务器中没有找到拥有身份组 `{discord_role.name}` 的用户。', ephemeral=True)
                self._log_slash_command(interaction, True)
                return

            # 连接数据库并执行同步操作
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            
            # 确保目标表存在
            if db_group == "admins":
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS admins (
                    id TEXT PRIMARY KEY
                )
                ''')
            elif db_group == "trusted_users":
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS trusted_users (
                    id TEXT PRIMARY KEY
                )
                ''')
            elif db_group == "kn_owner":
                cursor.execute('''
                CREATE TABLE IF NOT EXISTS kn_owner (
                    id TEXT PRIMARY KEY
                )
                ''')

            # 记录操作结果
            added_users = []
            already_exists_users = []
            
            for user_id in users_with_role:
                user_id_str = str(user_id)
                
                # 检查用户是否已在指定组中
                cursor.execute(f"SELECT id FROM {db_group} WHERE id = ?", (user_id_str,))
                if cursor.fetchone():
                    already_exists_users.append(user_id)
                else:
                    # 添加用户到指定组
                    cursor.execute(f"INSERT INTO {db_group} (id) VALUES (?)", (user_id_str,))
                    added_users.append(user_id)

            # 提交事务
            conn.commit()
            conn.close()
            
            # 更新机器人内存中的数据
            self._update_bot_data()
            
            # 构建结果消息
            result_message = f"✅ **身份组同步完成**\n"
            result_message += f"📋 **Discord身份组**: `{discord_role.name}` (ID: {role_id})\n"
            result_message += f"🎯 **目标数据库组**: `{db_group}`\n\n"
            
            if added_users:
                result_message += f"➕ **新增用户** ({len(added_users)}个):\n"
                for user_id in added_users[:10]:  # 最多显示10个
                    user = guild.get_member(user_id)
                    user_name = user.display_name if user else f"用户ID: {user_id}"
                    result_message += f"  • {user_name} (`{user_id}`)\n"
                if len(added_users) > 10:
                    result_message += f"  • ... 还有 {len(added_users) - 10} 个用户\n"
                result_message += "\n"
            
            if already_exists_users:
                result_message += f"ℹ️ **已存在用户** ({len(already_exists_users)}个):\n"
                for user_id in already_exists_users[:5]:  # 最多显示5个
                    user = guild.get_member(user_id)
                    user_name = user.display_name if user else f"用户ID: {user_id}"
                    result_message += f"  • {user_name} (`{user_id}`)\n"
                if len(already_exists_users) > 5:
                    result_message += f"  • ... 还有 {len(already_exists_users) - 5} 个用户\n"
            
            if not added_users and not already_exists_users:
                result_message += "ℹ️ 没有找到需要处理的用户。"
            
            await interaction.followup.send(result_message, ephemeral=True)
            self._log_slash_command(interaction, True)
            
            # 控制台日志
            print(f"👑 管理员 {interaction.user.name} ({interaction.user.id}) 执行了身份组同步:")
            print(f"   Discord身份组: {discord_role.name} (ID: {role_id})")
            print(f"   目标数据库组: {db_group}")
            print(f"   新增用户: {len(added_users)}个")
            print(f"   已存在用户: {len(already_exists_users)}个")
            
        except sqlite3.Error as e:
            await interaction.followup.send(f'❌ 数据库操作失败: {e}', ephemeral=True)
            print(f" [31m[错误] [0m 身份组同步数据库操作失败: {e}")
            self._log_slash_command(interaction, False)
        except Exception as e:
            await interaction.followup.send(f'❌ 执行身份组同步时发生未知错误: {e}', ephemeral=True)
            print(f" [31m[错误] [0m 身份组同步发生未知错误: {e}")
            self._log_slash_command(interaction, False)

    @syncrole.error
    async def on_syncrole_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """处理 syncrole 命令的特定错误"""
        # 检查interaction是否已经被响应过
        if interaction.response.is_done():
            print(f' syncrole命令错误已被处理: {error}')
            return
            
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message('❌ 您没有权限使用此命令。只有管理员可以执行身份组同步。', ephemeral=True)
        else:
            print(f' 未处理的斜杠命令错误 in RoleSyncCog: {error}')
            await interaction.response.send_message('❌ 执行命令时发生未知错误。', ephemeral=True)
        # 在任何错误情况下都记录失败
        self._log_slash_command(interaction, False)

async def setup(bot: commands.Bot):
    await bot.add_cog(RoleSyncCog(bot))

