import discord
from discord.ext import commands
from discord import app_commands
import os
import asyncio
from datetime import datetime

def is_admin_or_kn_owner(interaction: discord.Interaction) -> bool:
    """检查用户是否为管理员或知识库所有者，并验证kn_owner用户的子区权限"""
    user_id = interaction.user.id
    is_admin = user_id in interaction.client.admins
    is_kn_owner = user_id in getattr(interaction.client, 'kn_owner', [])
    
    # 管理员有全部权限
    if is_admin:
        return True
    
    # 非kn_owner用户无权限
    if not is_kn_owner:
        return False
    
    # kn_owner用户需要验证子区所有权
    # 检查子区是否为论坛频道的帖子
    if hasattr(interaction.channel, 'parent') and interaction.channel.parent:
        # 获取帖子的创建者（LZ）
        if hasattr(interaction.channel, 'owner_id'):
            thread_owner_id = interaction.channel.owner_id
            return thread_owner_id == user_id
        else:
            return False
    else:
        return False

class KnowledgeUploadCog(commands.Cog):
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

    @app_commands.command(name='上传知识库', description='[仅管理员/知识库所有者] 上传知识库文件')
    @app_commands.describe(file='要上传的txt格式知识库文件')
    @app_commands.check(is_admin_or_kn_owner)
    async def upload_knowledge(self, interaction: discord.Interaction, file: discord.Attachment):
        """上传知识库文件，只有管理员和知识库所有者可以使用"""
        
        try:
            # 检查文件格式
            if not file.filename.lower().endswith('.txt'):
                await interaction.response.send_message('❌ 只能上传txt格式的文件！', ephemeral=True)
                self._log_slash_command(interaction, False)
                return
            
            # 检查文件大小（限制为10MB）
            if file.size > 10 * 1024 * 1024:
                await interaction.response.send_message('❌ 文件大小不能超过10MB！', ephemeral=True)
                self._log_slash_command(interaction, False)
                return
            
            # 创建uploaded_prompt文件夹（如果不存在）
            upload_dir = 'uploaded_prompt'
            if not os.path.exists(upload_dir):
                try:
                    os.makedirs(upload_dir)
                except OSError as e:
                    await interaction.response.send_message(f'❌ 创建上传文件夹失败: {e}', ephemeral=True)
                    self._log_slash_command(interaction, False)
                    return
            
            # 生成文件名（使用频道ID）
            channel_id = interaction.channel.id
            output_filename = f"{channel_id}.txt"
            output_path = os.path.join(upload_dir, output_filename)
            
            # 读取上传的文件内容
            try:
                file_content = await file.read()
                file_text = file_content.decode('utf-8')
            except UnicodeDecodeError:
                await interaction.response.send_message('❌ 文件编码错误，请确保文件为UTF-8编码的文本文件！', ephemeral=True)
                self._log_slash_command(interaction, False)
                return
            except Exception as e:
                await interaction.response.send_message(f'❌ 读取文件失败: {e}', ephemeral=True)
                self._log_slash_command(interaction, False)
                return
            
            # 写入文件到uploaded_prompt文件夹
            try:
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(file_text)
                
                # 成功响应
                file_size_kb = len(file_text.encode('utf-8')) / 1024
                await interaction.response.send_message(
                    f'✅ 知识库文件上传成功！\n'
                    f'📁 文件名: `{output_filename}`\n'
                    f'📊 文件大小: `{file_size_kb:.2f} KB`\n'
                    f'👤 上传者: {interaction.user.mention}',
                    ephemeral=True
                )
                self._log_slash_command(interaction, True)
                
            except Exception as e:
                await interaction.response.send_message(f'❌ 保存文件失败: {e}', ephemeral=True)
                self._log_slash_command(interaction, False)
                return
                
        except Exception as e:
            # 处理未预期的异常
            await interaction.response.send_message(f'❌ 处理文件时发生未知错误: {e}', ephemeral=True)
            self._log_slash_command(interaction, False)
    
    @upload_knowledge.error
    async def on_upload_knowledge_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """处理上传知识库命令的错误"""
        # 检查interaction是否已被响应，避免重复响应
        if interaction.response.is_done():
            self._log_slash_command(interaction, False)
            return
            
        if isinstance(error, app_commands.CheckFailure):
            user_id = interaction.user.id
            is_admin = user_id in self.bot.admins
            is_kn_owner = user_id in getattr(self.bot, 'kn_owner', [])
            
            if not (is_admin or is_kn_owner):
                await interaction.response.send_message('❌ 您没有权限！只有管理员和知识库所有者可以上传知识库文件。', ephemeral=True)
            elif is_kn_owner and not is_admin:
                # 检查是否在论坛帖子中
                if not (hasattr(interaction.channel, 'parent') and interaction.channel.parent):
                    await interaction.response.send_message('❌ 此命令只能在论坛帖子中使用。', ephemeral=True)
                elif not hasattr(interaction.channel, 'owner_id'):
                    await interaction.response.send_message('❌ 无法验证子区作者信息。', ephemeral=True)
                else:
                    await interaction.response.send_message('❌ 权限验证失败：您只能在自己创建的子区中使用此命令。', ephemeral=True)
            else:
                await interaction.response.send_message('❌ 权限验证失败。', ephemeral=True)
            self._log_slash_command(interaction, False)
        else:
            await interaction.response.send_message(f'❌ 命令执行时发生错误: {error}', ephemeral=True)
            self._log_slash_command(interaction, False)

async def setup(bot: commands.Bot):
    await bot.add_cog(KnowledgeUploadCog(bot))
