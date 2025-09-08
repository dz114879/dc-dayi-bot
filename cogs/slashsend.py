import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime
import re
from cogs.logger import log_slash_command

class SlashSend(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def is_admin(self, interaction: discord.Interaction) -> bool:
        """检查用户是否为管理员"""
        return interaction.user.id in self.bot.admins

    def parse_message_link(self, message_link: str) -> tuple:
        """
        解析Discord消息链接，返回(guild_id, channel_id, message_id)
        支持格式: https://discord.com/channels/guild_id/channel_id/message_id
        """
        pattern = r'https://discord\.com/channels/(\d+)/(\d+)/(\d+)'
        match = re.match(pattern, message_link)
        if match:
            return int(match.group(1)), int(match.group(2)), int(match.group(3))
        return None, None, None

    @app_commands.command(name='send', description='[仅管理员] 发送消息或回复指定消息')
    @app_commands.describe(
        content='要发送的文字内容',
        message_link='（可选）要回复的消息链接'
    )
    async def send_message(self, interaction: discord.Interaction, content: str, message_link: str = None):
        """
        发送消息或回复指定消息的斜杠指令
        仅限管理员使用
        """
        # 检查管理员权限
        if not self.is_admin(interaction):
            await interaction.response.send_message('❌ 此命令仅限管理员使用。', ephemeral=True)
            log_slash_command(interaction, False)
            return

        try:
            # 如果没有提供消息链接，直接在当前频道发送消息
            if not message_link:
                await interaction.response.send_message(content)
                log_slash_command(interaction, True)
                print(f"👑 管理员 {interaction.user.name} ({interaction.user.id}) 在频道 {interaction.channel.name} 发送了消息")
                return

            # 解析消息链接
            guild_id, channel_id, message_id = self.parse_message_link(message_link.strip())
            
            if not all([guild_id, channel_id, message_id]):
                await interaction.response.send_message(
                    '❌ 无效的消息链接格式。请提供有效的Discord消息链接。\n'
                    '格式示例: `https://discord.com/channels/服务器ID/频道ID/消息ID`',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return

            # 获取目标服务器
            target_guild = self.bot.get_guild(guild_id)
            if not target_guild:
                await interaction.response.send_message('❌ 无法找到指定的服务器。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 获取目标频道
            target_channel = target_guild.get_channel(channel_id)
            if not target_channel:
                await interaction.response.send_message('❌ 无法找到指定的频道。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 检查机器人是否有发送消息的权限
            if not target_channel.permissions_for(target_guild.me).send_messages:
                await interaction.response.send_message('❌ 机器人在目标频道没有发送消息的权限。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 获取目标消息
            try:
                target_message = await target_channel.fetch_message(message_id)
            except discord.NotFound:
                await interaction.response.send_message('❌ 无法找到指定的消息。', ephemeral=True)
                log_slash_command(interaction, False)
                return
            except discord.Forbidden:
                await interaction.response.send_message('❌ 机器人没有权限访问该消息。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 回复目标消息
            await target_message.reply(content)
            
            # 发送成功确认
            await interaction.response.send_message(
                f'✅ 已成功回复消息！\n'
                f'**目标服务器**: {target_guild.name}\n'
                f'**目标频道**: {target_channel.mention}\n'
                f'**回复内容**: {content[:100]}{"..." if len(content) > 100 else ""}',
                ephemeral=True
            )
            log_slash_command(interaction, True)
            print(f"👑 管理员 {interaction.user.name} 回复了消息 {message_link}")

        except discord.HTTPException as e:
            await interaction.response.send_message(f'❌ 发送消息时发生错误: {e}', ephemeral=True)
            log_slash_command(interaction, False)
        except Exception as e:
            print(f"[错误] /send 命令执行时发生错误: {e}")
            await interaction.response.send_message('❌ 执行命令时发生未知错误。', ephemeral=True)
            log_slash_command(interaction, False)

    async def safe_defer(self, interaction: discord.Interaction):
        """
        安全的延迟响应函数
        检查交互是否已被响应，如果没有，就立即以"仅自己可见"的方式延迟响应
        """
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    @app_commands.command(name='hzhv', description='[仅管理员] 删除机器人消息')
    @app_commands.describe(
        message_link='（可选）要删除的消息链接，留空则删除机器人在当前频道的最后一条消息'
    )
    async def delete_message(self, interaction: discord.Interaction, message_link: str = None):
        """
        删除机器人消息的斜杠指令
        仅限管理员使用
        """
        # 先延迟响应，避免超时
        await self.safe_defer(interaction)
        
        # 检查管理员权限
        if not self.is_admin(interaction):
            await interaction.followup.send('❌ 此命令仅限管理员使用。', ephemeral=True)
            log_slash_command(interaction, False)
            return

        try:
            # 如果没有提供消息链接，删除机器人在当前频道的最后一条消息
            if not message_link:
                # 获取当前频道
                channel = interaction.channel
                
                # 搜索机器人在当前频道的最后一条消息
                bot_message = None
                async for message in channel.history(limit=100):
                    if message.author.id == self.bot.user.id:
                        bot_message = message
                        break
                
                if not bot_message:
                    await interaction.followup.send(
                        '❌ 在当前频道未找到机器人的消息（搜索了最近100条消息）。',
                        ephemeral=True
                    )
                    log_slash_command(interaction, False)
                    return
                
                # 删除找到的消息
                try:
                    await bot_message.delete()
                    await interaction.followup.send(
                        f'✅ 已成功删除机器人在 {channel.mention} 的最后一条消息。',
                        ephemeral=True
                    )
                    log_slash_command(interaction, True)
                    print(f"👑 管理员 {interaction.user.name} ({interaction.user.id}) 删除了机器人在频道 {channel.name} 的最后一条消息")
                except discord.Forbidden:
                    await interaction.followup.send('❌ 机器人没有删除该消息的权限。', ephemeral=True)
                    log_slash_command(interaction, False)
                except discord.NotFound:
                    await interaction.followup.send('❌ 消息已经被删除或不存在。', ephemeral=True)
                    log_slash_command(interaction, False)
                
                return

            # 解析消息链接
            guild_id, channel_id, message_id = self.parse_message_link(message_link.strip())
            
            if not all([guild_id, channel_id, message_id]):
                await interaction.followup.send(
                    '❌ 无效的消息链接格式。请提供有效的Discord消息链接。\n'
                    '格式示例: `https://discord.com/channels/服务器ID/频道ID/消息ID`',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return

            # 获取目标服务器
            target_guild = self.bot.get_guild(guild_id)
            if not target_guild:
                await interaction.followup.send('❌ 无法找到指定的服务器。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 获取目标频道
            target_channel = target_guild.get_channel(channel_id)
            if not target_channel:
                await interaction.followup.send('❌ 无法找到指定的频道。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 获取目标消息
            try:
                target_message = await target_channel.fetch_message(message_id)
            except discord.NotFound:
                await interaction.followup.send('❌ 无法找到指定的消息。', ephemeral=True)
                log_slash_command(interaction, False)
                return
            except discord.Forbidden:
                await interaction.followup.send('❌ 机器人没有权限访问该消息。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 检查消息是否是机器人发送的
            if target_message.author.id != self.bot.user.id:
                await interaction.followup.send(
                    '❌ 只能删除机器人自己发送的消息。',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return

            # 删除目标消息
            try:
                await target_message.delete()
                await interaction.followup.send(
                    f'✅ 已成功删除消息！\n'
                    f'**所在服务器**: {target_guild.name}\n'
                    f'**所在频道**: {target_channel.mention}',
                    ephemeral=True
                )
                log_slash_command(interaction, True)
                print(f"👑 管理员 {interaction.user.name} 删除了消息 {message_link}")
            except discord.Forbidden:
                await interaction.followup.send('❌ 机器人没有删除该消息的权限。', ephemeral=True)
                log_slash_command(interaction, False)
            except discord.NotFound:
                await interaction.followup.send('❌ 消息已经被删除或不存在。', ephemeral=True)
                log_slash_command(interaction, False)

        except discord.HTTPException as e:
            await interaction.followup.send(f'❌ 删除消息时发生错误: {e}', ephemeral=True)
            log_slash_command(interaction, False)
        except Exception as e:
            print(f"[错误] /hzhv 命令执行时发生错误: {e}")
            await interaction.followup.send('❌ 执行命令时发生未知错误。', ephemeral=True)
            log_slash_command(interaction, False)

async def setup(bot: commands.Bot):
    """设置并加载 SlashSend Cog"""
    await bot.add_cog(SlashSend(bot))