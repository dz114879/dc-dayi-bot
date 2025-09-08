import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import os
from datetime import datetime

def is_admin(interaction: discord.Interaction) -> bool:
    """检查用户是否为机器人的管理员"""
    return interaction.user.id in interaction.client.admins

class UsersDatabaseCog(commands.Cog):
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

    @app_commands.command(name='permission', description='[仅管理员] 管理用户权限组')
    @app_commands.describe(
        user_id='要操作的Discord用户ID（多个ID用英文逗号分隔）',
        group='权限组',
        action='操作类型'
    )
    @app_commands.choices(group=[
        app_commands.Choice(name="admins", value="admins"),
        app_commands.Choice(name="trusted_users", value="trusted_users"),
        app_commands.Choice(name="kn_owner", value="kn_owner")
    ])
    @app_commands.choices(action=[
        app_commands.Choice(name="增加", value="add"),
        app_commands.Choice(name="删除", value="remove")
    ])
    @app_commands.check(is_admin)
    async def permission(self, interaction: discord.Interaction, user_id: str, group: str, action: str):
        """管理用户权限组，只有管理员可以使用"""
        
        # 解析多个用户ID
        user_ids_str = [uid.strip() for uid in user_id.split(',') if uid.strip()]
        
        if not user_ids_str:
            await interaction.response.send_message('❌ 请提供至少一个有效的用户ID。', ephemeral=True)
            self._log_slash_command(interaction, False)
            return
        
        # 验证所有用户ID格式
        target_user_ids = []
        for uid_str in user_ids_str:
            try:
                target_user_id = int(uid_str)
                target_user_ids.append(target_user_id)
            except ValueError:
                await interaction.response.send_message(f'❌ 无效的用户ID格式: `{uid_str}`。请输入有效的数字ID。', ephemeral=True)
                self._log_slash_command(interaction, False)
                return

        # 防止管理员删除自己的管理员权限
        if action == "remove" and group == "admins" and interaction.user.id in target_user_ids:
            await interaction.response.send_message('❌ 您不能删除自己的管理员权限。', ephemeral=True)
            self._log_slash_command(interaction, False)
            return

        # 防止管理员互相删除对方的管理员权限
        if action == "remove" and group == "admins":
            # 检查要删除的用户中是否有其他管理员
            try:
                conn = sqlite3.connect('users.db')
                cursor = conn.cursor()
                
                # 获取所有管理员ID
                cursor.execute("SELECT id FROM admins")
                all_admins = [int(row[0]) for row in cursor.fetchall()]
                conn.close()
                
                # 检查目标用户中是否有其他管理员
                target_admins = [uid for uid in target_user_ids if uid in all_admins and uid != interaction.user.id]
                
                if target_admins:
                    if len(target_admins) == 1:
                        await interaction.response.send_message(
                            f'❌ 您不能删除其他管理员的权限。用户 `{target_admins[0]}` 是管理员。',
                            ephemeral=True
                        )
                    else:
                        admin_list = "`, `".join(str(uid) for uid in target_admins)
                        await interaction.response.send_message(
                            f'❌ 您不能删除其他管理员的权限。以下用户是管理员：`{admin_list}`',
                            ephemeral=True
                        )
                    self._log_slash_command(interaction, False)
                    return
                    
            except sqlite3.Error as e:
                await interaction.response.send_message(f'❌ 检查管理员权限时出错: {e}', ephemeral=True)
                self._log_slash_command(interaction, False)
                return

        try:
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            
            # 记录操作结果
            success_users = []
            failed_users = []
            already_exists_users = []
            not_exists_users = []
            
            # 对每个用户ID执行操作
            for target_user_id in target_user_ids:
                # 检查用户是否已在指定组中
                cursor.execute(f"SELECT id FROM {group} WHERE id = ?", (str(target_user_id),))
                user_exists = cursor.fetchone() is not None
                
                if action == "add":
                    if user_exists:
                        already_exists_users.append(str(target_user_id))
                    else:
                        # 添加用户到指定组
                        cursor.execute(f"INSERT INTO {group} (id) VALUES (?)", (str(target_user_id),))
                        success_users.append(str(target_user_id))
                        print(f"👑 管理员 {interaction.user.name} ({interaction.user.id}) 将用户 {target_user_id} 添加到 {group} 组。")
                        
                elif action == "remove":
                    if not user_exists:
                        not_exists_users.append(str(target_user_id))
                    else:
                        # 从指定组中删除用户
                        cursor.execute(f"DELETE FROM {group} WHERE id = ?", (str(target_user_id),))
                        success_users.append(str(target_user_id))
                        print(f"👑 管理员 {interaction.user.name} ({interaction.user.id}) 将用户 {target_user_id} 从 {group} 组中删除。")
            
            # 提交所有更改
            conn.commit()
            conn.close()
            
            # 更新机器人内存数据
            if success_users:
                self._update_bot_data()
            
            # 创建结果消息
            embed = discord.Embed(
                title="📊 权限操作结果",
                color=discord.Color.green() if success_users else discord.Color.orange()
            )
            
            if success_users:
                action_text = "增加" if action == "add" else "删除"
                embed.add_field(
                    name=f"✅ 成功{action_text} ({len(success_users)}个用户)",
                    value="`" + "`, `".join(success_users) + "`",
                    inline=False
                )
            
            if already_exists_users:
                embed.add_field(
                    name=f"⚠️ 已在 `{group}` 组中 ({len(already_exists_users)}个用户)",
                    value="`" + "`, `".join(already_exists_users) + "`",
                    inline=False
                )
            
            if not_exists_users:
                embed.add_field(
                    name=f"⚠️ 不在 `{group}` 组中 ({len(not_exists_users)}个用户)",
                    value="`" + "`, `".join(not_exists_users) + "`",
                    inline=False
                )
            
            embed.add_field(name="操作", value="增加" if action == "add" else "删除", inline=True)
            embed.add_field(name="权限组", value=group, inline=True)
            embed.set_footer(text=f"操作由管理员 {interaction.user.name} 执行")
            
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 记录操作结果
            if success_users or not (already_exists_users or not_exists_users):
                self._log_slash_command(interaction, True)
            else:
                self._log_slash_command(interaction, False)
            
        except sqlite3.Error as e:
            await interaction.response.send_message(f'❌ 数据库操作失败: {e}', ephemeral=True)
            print(f" [31m[错误] [0m 权限管理操作失败: {e}")
            self._log_slash_command(interaction, False)
        except Exception as e:
            await interaction.response.send_message(f'❌ 执行权限操作时发生未知错误: {e}', ephemeral=True)
            print(f" [31m[错误] [0m 权限管理未知错误: {e}")
            self._log_slash_command(interaction, False)

    @permission.error
    async def on_permission_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """处理 permission 命令的特定错误"""
        # 检查interaction是否已经被响应过
        if interaction.response.is_done():
            print(f' permission命令错误已被处理: {error}')
            return
            
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message('❌ 您没有权限使用此命令。只有管理员可以管理用户权限。', ephemeral=True)
        else:
            print(f' 未处理的斜杠命令错误 in UsersDatabaseCog: {error}')
            await interaction.response.send_message('❌ 执行命令时发生未知错误。', ephemeral=True)
        # 在任何错误情况下都记录失败
        self._log_slash_command(interaction, False)

async def setup(bot: commands.Bot):
    await bot.add_cog(UsersDatabaseCog(bot))