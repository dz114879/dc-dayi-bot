import discord
from discord.ext import commands
from discord import app_commands
import os
import openai
import asyncio
import mimetypes
import base64
from datetime import datetime
import time
import json
from typing import Optional, List
from cogs.rag_processor import RAGProcessor
from PIL import Image
import io

# --- 从 bot.py 引入的辅助函数和类 ---

class QuotaError(app_commands.AppCommandError):
    """自定义异常，用于表示用户配额不足"""
    pass

class ParallelLimitError(app_commands.AppCommandError):
    """自定义异常，用于表示并发达到上限"""
    pass

def encode_image_to_base64(image_path):
    """将图片文件编码为Base64数据URI。"""
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        mime_type = "application/octet-stream"
    with open(image_path, "rb") as image_file:
        base64_encoded_data = base64.b64encode(image_file.read()).decode('utf-8')
    return f"data:{mime_type};base64,{base64_encoded_data}"

# --- Cog 主体 ---

class KnownerDayi(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        # 初始化RAG处理器（如果启用）
        self.rag_processor = None
        if os.getenv("RAG_ENABLED", "false").lower() == "true":
            try:
                self.rag_processor = RAGProcessor()
                print("✅ [子区答疑] RAG系统已启用并初始化")
            except Exception as e:
                print(f"⚠️ [子区答疑] RAG系统初始化失败: {e}")
                self.rag_processor = None
        else:
            print("ℹ️ [子区答疑] RAG系统未启用")
            
        # 将上下文菜单命令添加到 bot 的 tree 中
        self.ctx_menu = app_commands.ContextMenu(
            name='子区内快速答疑',
            callback=self.quick_dayi,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self):
        """Cog 卸载时移除命令"""
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
    
    def _get_file_size_kb(self, file_path: str) -> float:
        """
        获取文件大小（KB）
        
        Args:
            file_path: 文件路径
            
        Returns:
            文件大小（KB）
        """
        if os.path.exists(file_path):
            return os.path.getsize(file_path) / 1024
        return 0
    
    async def _compress_image(self, image_path: str, max_size_kb: int = 250) -> str:
        """
        压缩图片到指定大小以下
        
        Args:
            image_path: 原始图片路径
            max_size_kb: 最大文件大小（KB），默认250KB
            
        Returns:
            压缩后的图片路径（如果需要压缩）或原始路径
        """
        try:
            # 检查原始文件大小
            original_size_kb = self._get_file_size_kb(image_path)
            print(f"🖼️ [子区答疑] 原始图片大小: {original_size_kb:.2f}KB")
            
            # 如果小于限制，直接返回
            if original_size_kb <= max_size_kb:
                print(f"✅ [子区答疑] 图片大小符合要求，无需压缩")
                return image_path
            
            # 需要压缩
            print(f"🔧 [子区答疑] 开始压缩图片 (目标: <{max_size_kb}KB)")
            
            # 打开图片
            with Image.open(image_path) as img:
                # 转换为RGB（如果是RGBA或其他格式）
                if img.mode in ('RGBA', 'LA', 'P'):
                    # 创建白色背景
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'RGBA' or img.mode == 'LA':
                        background.paste(img, mask=img.split()[-1])
                    else:
                        background.paste(img)
                    img = background
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                
                # 生成压缩后的文件路径
                base_name = os.path.splitext(image_path)[0]
                compressed_path = f"{base_name}_compressed.jpg"
                
                # 初始参数
                quality = 85
                max_dimension = 1920
                
                # 循环压缩直到满足大小要求
                for attempt in range(5):  # 最多尝试5次
                    # 调整尺寸
                    width, height = img.size
                    if width > max_dimension or height > max_dimension:
                        ratio = min(max_dimension / width, max_dimension / height)
                        new_width = int(width * ratio)
                        new_height = int(height * ratio)
                        resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                        print(f"  [子区答疑] 调整尺寸: {width}x{height} → {new_width}x{new_height}")
                    else:
                        resized_img = img
                    
                    # 保存到内存缓冲区以检查大小
                    buffer = io.BytesIO()
                    resized_img.save(buffer, format='JPEG', quality=quality, optimize=True)
                    buffer_size_kb = buffer.tell() / 1024
                    
                    print(f"  [子区答疑] 尝试 {attempt + 1}: 质量={quality}, 大小={buffer_size_kb:.2f}KB")
                    
                    # 如果满足要求，保存到文件
                    if buffer_size_kb <= max_size_kb:
                        buffer.seek(0)
                        with open(compressed_path, 'wb') as f:
                            f.write(buffer.read())
                        print(f"✅ [子区答疑] 压缩成功: {original_size_kb:.2f}KB → {buffer_size_kb:.2f}KB")
                        print(f"   压缩率: {(1 - buffer_size_kb/original_size_kb) * 100:.1f}%")
                        return compressed_path
                    
                    # 调整参数继续尝试
                    if attempt < 2:
                        quality -= 10  # 降低质量
                    else:
                        max_dimension = int(max_dimension * 0.8)  # 缩小尺寸
                        quality = 75  # 重置质量
                
                # 如果仍然无法满足要求，使用最后的尝试结果
                print(f"⚠️ [子区答疑] 无法压缩到{max_size_kb}KB以下，使用最佳尝试结果")
                buffer.seek(0)
                with open(compressed_path, 'wb') as f:
                    f.write(buffer.read())
                return compressed_path
                
        except Exception as e:
            print(f"❌ [子区答疑] 图片压缩失败: {e}")
            # 压缩失败时返回原始路径
            return image_path
    
    async def _describe_image(self, image_path: str) -> str:
        """
        使用图片描述模型生成图片的文本描述
        
        Args:
            image_path: 图片文件路径
            
        Returns:
            图片的文本描述
        """
        try:
            # 系统提示词
            system_prompt = """你是专业图片描述助手。请详细描述图片中的内容，包括：
- 主要对象
- 文字内容（如果有，请完整准确地提取，包括文字的颜色等）
- 技术细节（如代码、图表、UI界面、错误信息等）

用简洁准确的中文描述，重点关注可能与技术问题相关的内容。"""
            
            # 编码图片
            base64_image = encode_image_to_base64(image_path)
            
            # 构建请求
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": base64_image}}
                ]}
            ]
            
            # 调用API（使用IMAGE_DESCRIBE_MODEL）
            client = self.bot.openai_client
            loop = asyncio.get_event_loop()
            
            # 设置较短的超时时间（30秒）
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: client.chat.completions.create(
                        model=os.getenv("IMAGE_DESCRIBE_MODEL", "gemini-2.5-flash-lite-preview-06-17"),
                        messages=messages,
                        temperature=0.3,  # 较低的温度以获得更准确的描述
                        max_tokens=600
                    )
                ),
                timeout=30.0
            )
            
            description = response.choices[0].message.content
            print(f"🖼️ [子区答疑] 图片描述成功，长度: {len(description)}")
            return description
            
        except asyncio.TimeoutError:
            print("⚠️ [子区答疑] 图片描述超时（30秒）")
            raise Exception("图片描述超时")
        except Exception as e:
            print(f"❌ [子区答疑] 图片描述失败: {e}")
            raise Exception(f"图片描述失败: {str(e)}")
    
    async def _parallel_rag_retrieve_multiple_images(self, text: str, image_paths: List[str], compressed_paths: List[str] = None) -> List[dict]:
        """
        并行执行文本和多张图片的RAG检索
        
        Args:
            text: 文本内容
            image_paths: 图片文件路径列表（用于描述）
            compressed_paths: 压缩后的图片路径列表（可选，用于API调用）
            
        Returns:
            合并并去重后的检索结果
        """
        if not self.rag_processor:
            return []
            
        # 如果没有提供压缩路径，使用原始路径
        if compressed_paths is None:
            compressed_paths = image_paths
        
        tasks = []
        task_types = []
        
        # 任务1：文本RAG检索
        if text:
            print(f"📝 [子区答疑] 启动文本RAG检索任务")
            tasks.append(self.rag_processor.retrieve_context(text, mode="hybrid"))
            task_types.append("text")
        
        # 任务2-N：每张图片独立的描述 + RAG检索
        # 注意：这里使用压缩后的图片进行描述，以保证一致性
        for idx, img_path in enumerate(compressed_paths):
            if img_path and os.path.exists(img_path):
                async def image_to_rag(img_path, img_idx):
                    try:
                        print(f"🖼️ [子区答疑] 启动图片 {img_idx+1}/{len(image_paths)} 描述任务")
                        # 获取图片描述
                        description = await self._describe_image(img_path)
                        if description:
                            print(f"📝 [子区答疑] 使用图片 {img_idx+1} 的描述进行RAG检索")
                            # 使用描述进行RAG检索
                            return await self.rag_processor.retrieve_context(description, mode="hybrid")
                        else:
                            print(f"⚠️ [子区答疑] 图片 {img_idx+1} 描述无效，跳过RAG检索")
                            return []
                    except Exception as e:
                        print(f"❌ [子区答疑] 图片 {img_idx+1} 处理失败: {e}")
                        # 如果图片描述失败，抛出异常
                        raise e
                
                tasks.append(image_to_rag(img_path, idx))
                task_types.append(f"image_{idx+1}")
        
        # 如果没有任务，返回空结果
        if not tasks:
            return []
        
        # 并行执行所有任务
        print(f"⏳ [子区答疑] 并行执行 {len(tasks)} 个RAG检索任务...")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 检查是否有图片描述失败的情况
        for i, (result, task_type) in enumerate(zip(results, task_types)):
            if isinstance(result, Exception) and task_type.startswith("image_"):
                # 如果是图片任务失败了，抛出异常
                raise result
        
        # 收集所有检索结果
        all_contexts = []
        
        for i, (result, task_type) in enumerate(zip(results, task_types)):
            if isinstance(result, Exception):
                print(f"❌ [子区答疑] {task_type} 任务失败: {result}")
                continue
            
            if result:
                all_contexts.extend(result)
                print(f"✅ [子区答疑] {task_type} 检索到 {len(result)} 个文档块")
        
        # 去重和排序
        seen_texts = set()
        unique_contexts = []
        for ctx in sorted(all_contexts, key=lambda x: x.get('similarity', 0), reverse=True):
            # 用前200字符作为去重标识
            ctx_text = ctx['text'][:200] if 'text' in ctx else str(ctx)[:200]
            if ctx_text not in seen_texts:
                unique_contexts.append(ctx)
                seen_texts.add(ctx_text)
                # 限制最大文档数
                if len(unique_contexts) >= self.rag_processor.top_k:
                    break
        
        print(f"✅ [子区答疑] 合并去重后得到 {len(unique_contexts)} 个文档块")
        return unique_contexts

    async def quick_dayi(self, interaction: discord.Interaction, message: discord.Message):
        """
        对消息使用 /dayi 功能。
        提取消息中的文本和图片，调用 OpenAI API，并将结果公开回复。
        包含3分钟超时检测，超时后自动终止并提示用户。
        """
        user_id = interaction.user.id
        # --- 封禁检查 ---
        # 检查被引用消息的作者是否被封禁
        target_user = message.author
        target_user_id = str(target_user.id)  # 转换为字符串以匹配JSON格式
        
        # 从 banlist.json 加载封禁列表
        try:
            banlist_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'banlist.json')
            with open(banlist_path, 'r', encoding='utf-8') as f:
                banlist_data = json.load(f)
                
            # 检查用户是否在封禁列表中
            banned_user_info = None
            current_timestamp = datetime.now().timestamp()
            
            for ban_entry in banlist_data.get('banlist', []):
                if ban_entry['ID'] == target_user_id:
                    # 检查是否已经解封
                    unbanned_at = int(ban_entry['unbanned_at'])
                    if current_timestamp < unbanned_at:
                        banned_user_info = ban_entry
                        break
            
            if banned_user_info:
                # 格式化解封时间
                unbanned_timestamp = int(banned_user_info['unbanned_at'])
                unbanned_date = datetime.fromtimestamp(unbanned_timestamp)
                formatted_date = unbanned_date.strftime('%Y年%m月%d日 %H:%M:%S')
                
                # 构建封禁信息消息
                ban_message = (
                    f"❌ **该用户已被开发者封禁**\n\n"
                    f"**用户ID:** {banned_user_info['ID']}\n"
                    f"**封禁原因:** {banned_user_info['reason']}\n"
                    f"**解封时间:** {formatted_date}"
                )
                
                # 先发送私有响应
                await interaction.response.send_message("❌ 该用户已被封禁，无法对其使用子区内快速答疑功能。", ephemeral=True)
                
                # 在频道公开发送封禁消息（不使用embed）
                await interaction.channel.send(ban_message)
                
                print(f"🚫 [子区答疑] 尝试对封禁用户 {target_user_id} ({target_user.name}) 的消息使用子区内快速答疑")
                print(f"   封禁原因: {banned_user_info['reason']}")
                print(f"   解封时间: {formatted_date}")
                return
                
            # 调试日志
            print(f"✅ [子区答疑] 用户 {target_user_id} ({target_user.name}) 未被封禁")
            
        except FileNotFoundError:
            print("⚠️ [子区答疑] banlist.json 文件不存在，跳过封禁检查")
        except json.JSONDecodeError as e:
            print(f"❌ [子区答疑] 解析 banlist.json 失败: {e}")
        except Exception as e:
            print(f"❌ [子区答疑] 封禁检查出错: {e}")
            

        
        # --- 权限检查 ---
        is_admin = user_id in self.bot.admins
        is_kn_owner = user_id in getattr(self.bot, 'kn_owner', [])
        
        if not (is_admin or is_kn_owner):
            await interaction.response.send_message('❌ 没权。此命令仅限管理员和楼主使用。', ephemeral=True)
            return
        
        # --- kn_owner用户的子区作者验证 ---
        if is_kn_owner and not is_admin:
            # 获取当前子区ID
            channel_id = interaction.channel_id
            
            # 检查子区是否为论坛频道的帖子
            if hasattr(interaction.channel, 'parent') and interaction.channel.parent:
                # 获取帖子的创建者（LZ）
                if hasattr(interaction.channel, 'owner_id'):
                    thread_owner_id = interaction.channel.owner_id
                    if thread_owner_id != user_id:
                        await interaction.response.send_message('❌ 权限验证失败：您只能在自己创建的子区中使用此命令。', ephemeral=True)
                        return
                else:
                    await interaction.response.send_message('❌ 无法验证子区作者信息。', ephemeral=True)
                    return
            else:
                await interaction.response.send_message('❌ 此命令只能在论坛帖子中使用。', ephemeral=True)
                return
        
        # --- 并发检查 ---
        # 注意：这里我们假设 bot 实例上有一个 current_parallel_dayi_tasks 属性
        if not hasattr(self.bot, 'current_parallel_dayi_tasks'):
            self.bot.current_parallel_dayi_tasks = 0
        
        max_parallel = int(os.getenv("MAX_PARALLEL", 5))
        if self.bot.current_parallel_dayi_tasks >= max_parallel:
            await interaction.response.send_message(f"❌ 当前并发数已达上限 ({max_parallel})，请稍后再试。", ephemeral=True)
            return

        await interaction.response.send_message("⏳ 收到请求，正在处理中，请稍候...\n⏱️ 3分钟超时限制已启用", ephemeral=True)

        # --- 记录开始时间 ---
        start_time = time.time()
        timeout_seconds = 180  # 3分钟超时

        # --- 文件处理 ---
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base_filename = f"{timestamp}_{user_id}"
        temp_dir = 'app_temp'
        image_paths = []
        text_path = None
        
        # 提取消息文本
        text = message.content if message.content else "这是什么问题，怎么解决"
        
        # 提取消息中的所有图片附件
        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith('image/')]
        
        # 检查图片数量限制
        if len(image_attachments) > 3:
            await interaction.edit_original_response(
                content=f'❌ 图片数量超出限制！\n'
                       f'当前消息包含 **{len(image_attachments)}** 张图片，系统最多支持 **3** 张图片。\n'
                       f'请减少图片数量后重试。'
            )
            return

        try:
            self.bot.current_parallel_dayi_tasks += 1
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)

            # 保存文本
            text_path = os.path.join(temp_dir, f"{base_filename}.txt")
            with open(text_path, 'w', encoding='utf-8') as f:
                f.write(text)

            # 保存所有图片
            for idx, image_attachment in enumerate(image_attachments):
                _, image_extension = os.path.splitext(image_attachment.filename)
                image_path = os.path.join(temp_dir, f"{base_filename}_{idx}{image_extension}")
                await image_attachment.save(image_path)
                image_paths.append(image_path)
            
            if image_attachments:
                print(f"📸 [子区答疑] 保存了 {len(image_attachments)} 张图片")
        
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ 处理文件时出错: {e}")
            print(f" [31m[错误] [0m 用户 {user_id} 在 '快速答疑' 中保存文件时失败: {e}")
            self.bot.current_parallel_dayi_tasks -= 1
            return

        # --- OpenAI 请求 ---
        try:
            # 创建并行任务组
            parallel_tasks = {}
            compressed_paths = image_paths  # 默认使用原始路径
            
            # 如果有图片，创建压缩任务
            if image_paths:
                print(f"🚀 [子区答疑] 开始并行处理：图片压缩 + RAG检索...")
                parallel_tasks['compress'] = asyncio.gather(
                    *[self._compress_image(path) for path in image_paths]
                )
            
            # 构建四部分提示词
            channel_id = interaction.channel_id
            
            # 第1部分：顶部固定提示词
            owner_head_prompt = ""
            owner_head_file = "rag_prompt/owner_head.txt"
            try:
                with open(owner_head_file, 'r', encoding='utf-8') as f:
                    owner_head_prompt = f.read().strip()
                print(f"📖 [子区答疑] 加载顶部固定提示词")
            except Exception as e:
                print(f"⚠️ [子区答疑] 加载顶部固定提示词失败: {e}")
                owner_head_prompt = ""
            
            # 第2部分：基于向量化RAG检索的宽泛知识库内容
            rag_context = ""
            if self.rag_processor:
                try:
                    contexts = []
                    
                    # 判断是否有图片
                    if image_paths:
                        # 先等待压缩完成，然后使用压缩后的图片进行描述和RAG
                        if 'compress' in parallel_tasks:
                            compressed_paths = await parallel_tasks['compress']
                            print(f"✅ [子区答疑] 图片压缩完成")
                        
                        # 新流程：并行处理文本和多张图片（使用压缩后的图片）
                        print(f"🚀 [子区答疑] 开始并行RAG检索 - 文本长度: {len(text)}, 图片数量: {len(compressed_paths)}")
                        try:
                            contexts = await self._parallel_rag_retrieve_multiple_images(
                                text=text,
                                image_paths=image_paths,
                                compressed_paths=compressed_paths
                            )
                        except Exception as img_error:
                            # 如果图片描述失败，立即终止请求
                            error_msg = str(img_error)
                            if "图片描述" in error_msg:
                                await interaction.edit_original_response(
                                    content=f"❌ **图片处理失败**\n\n"
                                           f"{error_msg}\n\n"
                                           f"建议：\n"
                                           f"• 确保图片格式正确（支持JPG、PNG等常见格式）\n"
                                           f"• 减小图片尺寸\n"
                                           f"• 稍后重试"
                                )
                                return
                            else:
                                # 其他错误，记录但继续
                                print(f"❌ [子区答疑] RAG检索失败: {img_error}")
                                contexts = []
                    else:
                        # 纯文本：保持原流程
                        print(f"📝 [子区答疑] 开始纯文本检索 - 文本长度: {len(text)}")
                        contexts = await self.rag_processor.retrieve_context(text, mode="hybrid")
                        print(f"✅ [子区答疑] RAG文本检索到 {len(contexts)} 个相关文档块")
                    
                    # 构建RAG上下文部分
                    if contexts:
                        rag_parts = []
                        for i, ctx in enumerate(contexts, 1):
                            rag_parts.append(f"[RAG相关知识 {i}]\n{ctx['text']}\n")
                        rag_context = "\n[基于向量检索的相关知识]\n" + ''.join(rag_parts)
                    else:
                        print("⚠️ [子区答疑] RAG未检索到相关内容")
                        
                except Exception as e:
                    # 特殊处理图片描述相关的错误
                    if "图片描述" in str(e):
                        await interaction.edit_original_response(
                            content=f"❌ **图片处理失败**\n\n"
                                   f"{str(e)}\n\n"
                                   f"建议：\n"
                                   f"• 确保图片格式正确（支持JPG、PNG等常见格式）\n"
                                   f"• 减小图片尺寸\n"
                                   f"• 稍后重试"
                        )
                        return
                    print(f"❌ [子区答疑] RAG检索失败: {e}")
                    rag_context = ""
            
            # 第3部分：未经处理的子区专属知识库文件内容
            thread_specific_prompt = ""
            prompt_file = f"uploaded_prompt/{channel_id}.txt"
            try:
                with open(prompt_file, 'r', encoding='utf-8') as f:
                    thread_specific_prompt = f.read().strip()
                if not thread_specific_prompt:
                    thread_specific_prompt = ""
                else:
                    thread_specific_prompt = f"\n[子区专属知识库]\n{thread_specific_prompt}\n"
                    print(f"📖 [子区答疑] 加载子区专属知识库")
            except FileNotFoundError:
                await interaction.edit_original_response(
                    content=f"❌ 知识库文件不存在：{prompt_file}\n"
                           f"请先使用知识库上传功能为此子区上传对应的知识库文件。"
                )
                return
            except Exception as e:
                await interaction.edit_original_response(content=f"❌ 读取知识库文件时出错：{e}")
                return
            
            # 第4部分：用户提问内容（将在user角色中）
            
            # 组合系统提示词（前三部分）
            system_prompt = f"""{owner_head_prompt}
{rag_context}
{thread_specific_prompt}"""
            
            # 如果系统提示词为空，使用默认值
            if not system_prompt.strip():
                system_prompt = "You are a helpful assistant."
            
            # 如果还没有执行压缩，现在执行（处理没有RAG的情况）
            if image_paths and 'compress' in parallel_tasks and compressed_paths == image_paths:
                compressed_paths = await parallel_tasks['compress']
                print(f"✅ [子区答疑] 图片压缩完成")
            
            # 使用压缩后的路径替换原始路径
            if compressed_paths != image_paths:
                image_paths = compressed_paths
            
            # 构建请求内容
            user_content = [{"type": "text", "text": text}]
            # 添加所有图片到请求中（使用压缩后的图片）
            for image_path in image_paths:
                # 打印每个图片的最终大小
                size_kb = self._get_file_size_kb(image_path)
                print(f"📎 [子区答疑] 添加图片到API请求: {os.path.basename(image_path)} ({size_kb:.2f}KB)")
                
                base64_image = encode_image_to_base64(image_path)
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": base64_image}
                })
            
            # 计算总大小
            if image_paths:
                total_size_kb = sum(self._get_file_size_kb(path) for path in image_paths)
                print(f"📊 [子区答疑] API请求图片总大小: {total_size_kb:.2f}KB")

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ]

            client = self.bot.openai_client # 假设 client 在 bot 实例上
            
            # 计算剩余超时时间
            elapsed_time = time.time() - start_time
            remaining_timeout = max(timeout_seconds - elapsed_time, 1)
            
            # 定义异步API调用任务
            async def make_api_call():
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(
                    None,
                    lambda: client.chat.completions.create(
                        model=os.getenv("OPENAI_MODEL"),
                        messages=messages,
                        temperature=1.0,
                        stream=False
                    )
                )
            
            # 使用 asyncio.wait_for 实现超时控制
            try:
                response = await asyncio.wait_for(
                    make_api_call(),
                    timeout=remaining_timeout
                )
                ai_response = response.choices[0].message.content
                
                # --- 公开回复 ---
                # 创建回复内容的 Embed
                embed = discord.Embed(
                    title="🤖 AI 回复",
                    description=ai_response,
                    color=discord.Color.blue()
                )
                
                # 计算总用时
                total_time = time.time() - start_time
                embed.set_footer(text=f"由 {os.getenv('OPENAI_MODEL')} 提供支持 | 用时: {total_time:.1f}秒")
                
                # 回复原始消息
                await message.reply(embed=embed)
                
                # 编辑初始的临时消息，提示操作完成
                await interaction.edit_original_response(content=f"✅ 已成功回复。（用时: {total_time:.1f}秒）")
                
            except asyncio.TimeoutError:
                # 超时处理
                timeout_embed = discord.Embed(
                    title="⏱️ 答疑超时",
                    description="抱歉，本次答疑请求已超过3分钟时限，已自动终止。\n\n可能的原因：\n• AI服务响应缓慢\n• 网络连接问题\n• 请求内容过于复杂\n\n请稍后重试或简化您的问题。",
                    color=discord.Color.orange()
                )
                timeout_embed.set_footer(text=f"超时时间: {timeout_seconds}秒")
                
                # 回复原始消息（超时通知）
                await message.reply(embed=timeout_embed)
                
                # 编辑初始的临时消息
                await interaction.edit_original_response(content=f"❌ 答疑请求已因超时（{timeout_seconds}秒）被终止。")
                
                print(f" [33m[超时] [0m 用户 {user_id} 的快速答疑请求超时（{timeout_seconds}秒）")
                return

        except openai.APIConnectionError as e:
            await interaction.edit_original_response(content=f"❌ **连接错误**: 无法连接到AI服务。\n`{e}`")
        except openai.RateLimitError as e:
            await interaction.edit_original_response(content=f"❌ **请求超速**: 已达到API的请求频率限制。\n`{e}`")
        except openai.AuthenticationError as e:
            await interaction.edit_original_response(content=f"❌ **认证失败**: API密钥无效或已过期。\n`{e}`")
        except openai.APIStatusError as e:
            await interaction.edit_original_response(content=f"❌ **API 错误**: API返回了非200的状态码。\n状态码: {e.status_code}\n响应: {e.response}")
        except Exception as e:
            print(f" [31m[AI错误] [0m '快速答疑' 调用AI时发生错误: {e}")
            await interaction.edit_original_response(content=f"❌ 发生意外错误: {e}，请联系管理员。")
        
        finally:
            self.bot.current_parallel_dayi_tasks -= 1
            # 清理临时文件
            if os.getenv("DELETE_TEMP_FILES", "false").lower() == "true":
                # 清理文本文件
                if text_path and os.path.exists(text_path):
                    try:
                        os.remove(text_path)
                    except Exception as e:
                        print(f" [33m[警告] [0m 删除临时文件 {text_path} 时出错: {e}")
                
                # 收集所有需要清理的图片文件（包括原始和压缩的）
                all_image_paths = set()  # 使用set避免重复
                
                # 添加当前使用的图片路径（可能是压缩后的）
                for path in image_paths:
                    if path:
                        all_image_paths.add(path)
                
                # 添加原始图片路径（以防压缩后的路径不同）
                for idx, _ in enumerate(image_attachments):
                    _, image_extension = os.path.splitext(image_attachments[idx].filename)
                    original_path = os.path.join(temp_dir, f"{base_filename}_{idx}{image_extension}")
                    all_image_paths.add(original_path)
                    # 添加可能的压缩文件路径
                    compressed_path = f"{os.path.splitext(original_path)[0]}_compressed.jpg"
                    all_image_paths.add(compressed_path)
                
                # 清理所有图片文件
                for image_path in all_image_paths:
                    if image_path and os.path.exists(image_path):
                        try:
                            os.remove(image_path)
                            print(f"🗑️ [子区答疑] 已删除临时文件: {os.path.basename(image_path)}")
                        except Exception as e:
                            print(f" [33m[警告] [0m 删除临时文件 {image_path} 时出错: {e}")

async def setup(bot: commands.Bot):
    # 在 setup 函数中传递 bot 实例
    # 确保 bot.py 中的 client 被设置为 bot 的属性
    if not hasattr(bot, 'openai_client'):
         # 从 .env 文件加载配置
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        OPENAI_API_BASE_URL = os.getenv("OPENAI_API_BASE_URL")
        if not all([OPENAI_API_KEY, OPENAI_API_BASE_URL]):
            print(" [错误](来自App) 缺少必要的 OpenAI 环境变量。")
            bot.openai_client = None
        else:
            bot.openai_client = openai.OpenAI(
                api_key=OPENAI_API_KEY,
                base_url=OPENAI_API_BASE_URL,
            )

    await bot.add_cog(KnownerDayi(bot))