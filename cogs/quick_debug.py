import discord
from discord.ext import commands
from discord import app_commands
import os
import sys
import asyncio
import time
import json
import io
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Any
from collections import deque
import traceback
from PIL import Image
import base64
import mimetypes

# 导入必要的模块
from cogs.rag_processor import RAGProcessor

# --- 日志缓冲区系统 ---
class LogBuffer:
    """全局日志缓冲区，用于捕获所有控制台输出"""
    
    def __init__(self, max_size: int = 10000):
        """
        初始化日志缓冲区
        
        Args:
            max_size: 最大缓存日志条数
        """
        self.logs = deque(maxlen=max_size)
        self.original_stdout = None
        self.original_stderr = None
        self.enabled = False
        
    def write(self, message: str):
        """捕获并存储日志"""
        if message and message.strip():  # 忽略空消息
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            # 存储原始消息，保留格式
            self.logs.append(f"[{timestamp}] {message}")
        
        # 同时输出到原始控制台
        if self.original_stdout:
            self.original_stdout.write(message)
            self.original_stdout.flush()
    
    def error_write(self, message: str):
        """捕获并存储错误日志"""
        if message and message.strip():
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            self.logs.append(f"[{timestamp}] [ERROR] {message}")
        
        # 同时输出到原始错误流
        if self.original_stderr:
            self.original_stderr.write(message)
            self.original_stderr.flush()
    
    def flush(self):
        """刷新缓冲区（为了兼容性）"""
        if self.original_stdout:
            self.original_stdout.flush()
        if self.original_stderr:
            self.original_stderr.flush()
    
    def enable(self):
        """启用日志捕获"""
        if not self.enabled:
            self.original_stdout = sys.stdout
            self.original_stderr = sys.stderr
            
            # 创建包装器对象
            stdout_wrapper = self
            stderr_wrapper = type('StderrWrapper', (), {
                'write': lambda _, msg: self.error_write(msg),
                'flush': lambda _: self.flush()
            })()
            
            sys.stdout = stdout_wrapper
            sys.stderr = stderr_wrapper
            self.enabled = True
            print("✅ 日志缓冲系统已启用")
    
    def disable(self):
        """禁用日志捕获"""
        if self.enabled and self.original_stdout and self.original_stderr:
            sys.stdout = self.original_stdout
            sys.stderr = self.original_stderr
            self.enabled = False
            print("❌ 日志缓冲系统已禁用")
    
    def get_logs(self, count: int = 0) -> List[str]:
        """
        获取指定数量的日志
        
        Args:
            count: 要获取的日志条数，0表示全部
            
        Returns:
            日志列表
        """
        if count == 0:
            return list(self.logs)
        else:
            # 获取最近的count条日志
            return list(self.logs)[-count:] if count < len(self.logs) else list(self.logs)
    
    def clear(self):
        """清空日志缓冲区"""
        self.logs.clear()

# 创建全局日志缓冲区实例
global_log_buffer = LogBuffer()

# --- 辅助函数 ---
def encode_image_to_base64(image_path: str) -> str:
    """将图片文件编码为Base64数据URI"""
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        mime_type = "application/octet-stream"
    with open(image_path, "rb") as image_file:
        base64_encoded_data = base64.b64encode(image_file.read()).decode('utf-8')
    return f"data:{mime_type};base64,{base64_encoded_data}"

async def safe_defer(interaction: discord.Interaction):
    """安全的defer函数"""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

# --- 快速调试Cog ---
class QuickDebug(commands.Cog):
    """快速调试和测试功能"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        
        # 初始化RAG处理器（如果启用）
        self.rag_processor = None
        if os.getenv("RAG_ENABLED", "false").lower() == "true":
            try:
                self.rag_processor = RAGProcessor()
                print("✅ [QuickDebug] RAG系统已启用")
            except Exception as e:
                print(f"⚠️ [QuickDebug] RAG系统初始化失败: {e}")
                self.rag_processor = None
        else:
            print("ℹ️ [QuickDebug] RAG系统未启用")
        
        # 启用全局日志捕获
        global_log_buffer.enable()
        
        # 准备测试图片路径
        self.test_image_path = "test_assets/test_error.png"
        self._ensure_test_assets()
    
    def _ensure_test_assets(self):
        """确保测试资源存在"""
        test_dir = "test_assets"
        if not os.path.exists(test_dir):
            os.makedirs(test_dir)
            print(f"📁 创建测试资源目录: {test_dir}")
        
        # 如果测试图片不存在，创建一个简单的测试图片
        if not os.path.exists(self.test_image_path):
            try:
                # 创建一个包含错误信息的测试图片
                img = Image.new('RGB', (800, 600), color='white')
                from PIL import ImageDraw, ImageFont
                draw = ImageDraw.Draw(img)
                
                # 绘制错误信息
                error_text = "Error: Connection timeout\nETIMEDOUT at line 42\nPlease check network settings"
                try:
                    # 尝试使用默认字体
                    font = ImageFont.load_default()
                except:
                    font = None
                
                draw.text((50, 50), error_text, fill='red', font=font)
                img.save(self.test_image_path)
                print(f"✅ 创建测试图片: {self.test_image_path}")
            except Exception as e:
                print(f"⚠️ 创建测试图片失败: {e}")
    
    def cog_unload(self):
        """Cog卸载时的清理"""
        # 禁用日志捕获（可选）
        # global_log_buffer.disable()
        pass
    
    @app_commands.command(name='看看日志', description='[仅管理员] 导出最近的控制台日志')
    @app_commands.describe(count='要导出的日志条数，0表示全部，默认100')
    async def view_logs(self, interaction: discord.Interaction, count: int = 100):
        """导出控制台日志到文件"""
        
        # 🔥 黄金法则：永远先 defer！
        await safe_defer(interaction)
        
        # 权限检查
        if interaction.user.id not in self.bot.admins:
            await interaction.edit_original_response(
                content='❌ 此命令仅限管理员使用。'
            )
            return
        
        try:
            # 获取日志
            logs = global_log_buffer.get_logs(count)
            
            if not logs:
                await interaction.edit_original_response(
                    content='📭 当前没有日志记录。'
                )
                return
            
            # 生成日志文件
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"logs_{timestamp}.txt"
            
            # 创建日志内容
            log_content = f"=== Discord Bot 日志导出 ===\n"
            log_content += f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            log_content += f"导出用户: {interaction.user.name} ({interaction.user.id})\n"
            log_content += f"日志条数: {len(logs)}\n"
            log_content += "=" * 50 + "\n\n"
            
            # 添加日志内容
            for log in logs:
                log_content += log
                if not log.endswith('\n'):
                    log_content += '\n'
            
            # 创建文件对象
            file_buffer = io.BytesIO(log_content.encode('utf-8'))
            file_buffer.seek(0)
            discord_file = discord.File(file_buffer, filename=filename)
            
            # 创建嵌入消息
            embed = discord.Embed(
                title="📋 日志导出完成",
                description=f"已导出最近 **{len(logs)}** 条日志",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            embed.add_field(name="文件名", value=filename, inline=True)
            embed.add_field(name="日志大小", value=f"{len(log_content)} 字节", inline=True)
            embed.set_footer(text=f"操作者: {interaction.user.name}")
            
            # 发送文件
            await interaction.edit_original_response(
                content="✅ 日志导出成功！",
                embed=embed,
                attachments=[discord_file]
            )
            
            print(f"📋 管理员 {interaction.user.name} 导出了 {len(logs)} 条日志")
            
        except Exception as e:
            error_msg = f"导出日志时发生错误: {str(e)}"
            print(f"❌ {error_msg}")
            await interaction.edit_original_response(
                content=f"❌ {error_msg}"
            )
    
    @app_commands.command(name='快速测试', description='[仅管理员] 测试答疑系统各项功能')
    async def quick_test(self, interaction: discord.Interaction):
        """执行快速功能测试"""
        
        # 🔥 黄金法则：永远先 defer！
        await safe_defer(interaction)
        
        # 权限检查
        if interaction.user.id not in self.bot.admins:
            await interaction.edit_original_response(
                content='❌ 此命令仅限管理员使用。'
            )
            return
        
        # 开始测试
        await interaction.edit_original_response(
            content="🧪 开始执行快速测试，请稍候..."
        )
        
        test_start_time = time.perf_counter()
        test_results = {
            "executor": interaction.user.name,
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "text_test": {},
            "image_test": {},
            "system_status": {}
        }
        
        # --- 测试1: 纯文本快速答疑 ---
        try:
            print("🧪 [测试1] 开始纯文本答疑测试...")
            test_question = "请问酒馆是什么？"
            text_test_start = time.perf_counter()
            
            # 测试向量化和RAG检索
            if self.rag_processor:
                try:
                    # 向量化测试
                    embed_start = time.perf_counter()
                    test_embedding = await self.rag_processor.get_embeddings_batch([test_question])
                    embed_time = time.perf_counter() - embed_start
                    
                    test_results["text_test"]["embedding_status"] = "成功"
                    test_results["text_test"]["embedding_time"] = f"{embed_time:.2f}s"
                    
                    # RAG检索测试
                    rag_start = time.perf_counter()
                    contexts = await self.rag_processor.retrieve_context(test_question)
                    rag_time = time.perf_counter() - rag_start
                    
                    test_results["text_test"]["rag_status"] = "成功"
                    test_results["text_test"]["rag_time"] = f"{rag_time:.2f}s"
                    test_results["text_test"]["rag_results"] = len(contexts)
                    
                    if contexts:
                        test_results["text_test"]["max_similarity"] = f"{contexts[0]['similarity']:.2f}"
                        test_results["text_test"]["min_similarity"] = f"{contexts[-1]['similarity']:.2f}"
                    
                except Exception as e:
                    test_results["text_test"]["rag_error"] = str(e)
                    print(f"❌ RAG测试失败: {e}")
            else:
                test_results["text_test"]["rag_status"] = "未启用"
            
            # 测试主答疑API
            if self.bot.openai_client:
                try:
                    api_start = time.perf_counter()
                    
                    # 构建测试消息
                    messages = [
                        {"role": "system", "content": "你是一个测试助手。"},
                        {"role": "user", "content": test_question}
                    ]
                    
                    # 调用API
                    response = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: self.bot.openai_client.chat.completions.create(
                                model=os.getenv("OPENAI_MODEL"),
                                messages=messages,
                                max_tokens=100
                            )
                        ),
                        timeout=30.0
                    )
                    
                    api_time = time.perf_counter() - api_start
                    
                    if response and response.choices:
                        test_results["text_test"]["api_status"] = "成功"
                        test_results["text_test"]["api_time"] = f"{api_time:.2f}s"
                        test_results["text_test"]["response_length"] = len(response.choices[0].message.content)
                    else:
                        test_results["text_test"]["api_status"] = "空响应"
                        
                except asyncio.TimeoutError:
                    test_results["text_test"]["api_status"] = "超时"
                except Exception as e:
                    test_results["text_test"]["api_status"] = "失败"
                    test_results["text_test"]["api_error"] = str(e)
            
            text_total_time = time.perf_counter() - text_test_start
            test_results["text_test"]["total_time"] = f"{text_total_time:.2f}s"
            
        except Exception as e:
            test_results["text_test"]["error"] = str(e)
            print(f"❌ 纯文本测试失败: {e}")
        
        # --- 测试2: 带图片的快速答疑 ---
        try:
            print("🧪 [测试2] 开始带图片答疑测试...")
            image_test_start = time.perf_counter()
            
            if os.path.exists(self.test_image_path):
                # 测试图片压缩
                try:
                    compress_start = time.perf_counter()
                    
                    # 获取原始大小
                    original_size = os.path.getsize(self.test_image_path) / 1024  # KB
                    
                    # 模拟压缩过程
                    with Image.open(self.test_image_path) as img:
                        # 压缩到较小尺寸
                        max_size = 1024
                        if img.width > max_size or img.height > max_size:
                            ratio = min(max_size / img.width, max_size / img.height)
                            new_size = (int(img.width * ratio), int(img.height * ratio))
                            img = img.resize(new_size, Image.Resampling.LANCZOS)
                        
                        # 保存到内存
                        buffer = io.BytesIO()
                        img.save(buffer, format='JPEG', quality=85, optimize=True)
                        compressed_size = buffer.tell() / 1024  # KB
                    
                    compress_time = time.perf_counter() - compress_start
                    
                    test_results["image_test"]["compress_status"] = "成功"
                    test_results["image_test"]["compress_time"] = f"{compress_time:.2f}s"
                    test_results["image_test"]["original_size"] = f"{original_size:.1f}KB"
                    test_results["image_test"]["compressed_size"] = f"{compressed_size:.1f}KB"
                    
                except Exception as e:
                    test_results["image_test"]["compress_error"] = str(e)
                
                # 测试图片描述API
                if self.bot.openai_client:
                    try:
                        describe_start = time.perf_counter()
                        
                        # 编码图片
                        base64_image = encode_image_to_base64(self.test_image_path)
                        
                        # 调用描述API
                        messages = [
                            {"role": "system", "content": "描述这张图片的内容。"},
                            {"role": "user", "content": [
                                {"type": "image_url", "image_url": {"url": base64_image}}
                            ]}
                        ]
                        
                        response = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(
                                None,
                                lambda: self.bot.openai_client.chat.completions.create(
                                    model=os.getenv("IMAGE_DESCRIBE_MODEL", "gemini-2.5-flash-lite-preview-06-17"),
                                    messages=messages,
                                    max_tokens=200
                                )
                            ),
                            timeout=30.0
                        )
                        
                        describe_time = time.perf_counter() - describe_start
                        
                        if response and response.choices:
                            test_results["image_test"]["describe_status"] = "成功"
                            test_results["image_test"]["describe_time"] = f"{describe_time:.2f}s"
                            description = response.choices[0].message.content
                            test_results["image_test"]["description_length"] = len(description)
                            
                            # 如果有RAG，测试图片描述的RAG检索
                            if self.rag_processor and description:
                                rag_start = time.perf_counter()
                                img_contexts = await self.rag_processor.retrieve_context(description)
                                rag_time = time.perf_counter() - rag_start
                                
                                test_results["image_test"]["rag_time"] = f"{rag_time:.2f}s"
                                test_results["image_test"]["rag_results"] = len(img_contexts)
                        else:
                            test_results["image_test"]["describe_status"] = "空响应"
                            
                    except asyncio.TimeoutError:
                        test_results["image_test"]["describe_status"] = "超时"
                    except Exception as e:
                        test_results["image_test"]["describe_status"] = "失败"
                        test_results["image_test"]["describe_error"] = str(e)
            else:
                test_results["image_test"]["status"] = "测试图片不存在"
            
            image_total_time = time.perf_counter() - image_test_start
            test_results["image_test"]["total_time"] = f"{image_total_time:.2f}s"
            
        except Exception as e:
            test_results["image_test"]["error"] = str(e)
            print(f"❌ 图片测试失败: {e}")
        
        # --- 收集系统状态 ---
        try:
            # RAG系统状态
            if self.rag_processor:
                rag_stats = self.rag_processor.get_stats()
                test_results["system_status"]["rag_enabled"] = True
                test_results["system_status"]["total_chunks"] = rag_stats.get("total_chunks", 0)
                test_results["system_status"]["embedding_model"] = rag_stats.get("embedding_model", "unknown")
            else:
                test_results["system_status"]["rag_enabled"] = False
            
            # 并发状态
            test_results["system_status"]["current_tasks"] = getattr(self.bot, 'current_parallel_dayi_tasks', 0)
            test_results["system_status"]["max_parallel"] = int(os.getenv("MAX_PARALLEL", 5))
            
            # API配置
            test_results["system_status"]["main_model"] = os.getenv("OPENAI_MODEL", "未配置")
            test_results["system_status"]["image_model"] = os.getenv("IMAGE_DESCRIBE_MODEL", "未配置")
            
        except Exception as e:
            test_results["system_status"]["error"] = str(e)
        
        # 总测试时间
        total_test_time = time.perf_counter() - test_start_time
        test_results["total_time"] = f"{total_test_time:.2f}s"
        
        # --- 生成测试报告 ---
        report = self._generate_test_report(test_results)
        
        # 创建嵌入消息
        embed = discord.Embed(
            title="🧪 快速测试完成",
            description=report,
            color=discord.Color.green() if "失败" not in report else discord.Color.orange(),
            timestamp=datetime.now()
        )
        embed.set_footer(text=f"执行者: {interaction.user.name}")
        
        # 如果报告太长，保存为文件
        if len(report) > 4000:
            # 创建文件
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"test_report_{timestamp}.md"
            file_buffer = io.BytesIO(report.encode('utf-8'))
            file_buffer.seek(0)
            discord_file = discord.File(file_buffer, filename=filename)
            
            # 发送简短摘要和文件
            summary = self._generate_test_summary(test_results)
            embed.description = summary
            
            await interaction.edit_original_response(
                content="✅ 测试完成！",
                embed=embed,
                attachments=[discord_file]
            )
        else:
            await interaction.edit_original_response(
                content="✅ 测试完成！",
                embed=embed
            )
        
        print(f"🧪 管理员 {interaction.user.name} 执行了快速测试")
    
    def _generate_test_report(self, results: Dict) -> str:
        """生成测试报告"""
        report = []
        report.append("## 🧪 快速测试报告")
        report.append(f"**测试时间**: {results['timestamp']}")
        report.append(f"**执行者**: {results['executor']}")
        report.append("")
        
        # 纯文本测试结果
        report.append("### 📝 纯文本测试")
        text_test = results.get("text_test", {})
        
        if "error" in text_test:
            report.append(f"- ❌ 测试失败: {text_test['error']}")
        else:
            # 向量化
            if "embedding_status" in text_test:
                status = "✅" if text_test["embedding_status"] == "成功" else "❌"
                report.append(f"- {status} 向量化API: {text_test['embedding_status']} (耗时: {text_test.get('embedding_time', 'N/A')})")
            
            # RAG检索
            if "rag_status" in text_test:
                if text_test["rag_status"] == "成功":
                    report.append(f"- ✅ RAG检索: 找到 {text_test.get('rag_results', 0)} 个相关文档 (耗时: {text_test.get('rag_time', 'N/A')})")
                    if "max_similarity" in text_test:
                        report.append(f"  - 最高相似度: {text_test['max_similarity']}")
                        report.append(f"  - 最低相似度: {text_test['min_similarity']}")
                elif text_test["rag_status"] == "未启用":
                    report.append("- ℹ️ RAG系统: 未启用")
                else:
                    report.append(f"- ❌ RAG检索: {text_test.get('rag_error', '失败')}")
            
            # 主API
            if "api_status" in text_test:
                status = "✅" if text_test["api_status"] == "成功" else "❌"
                report.append(f"- {status} 答疑API: {text_test['api_status']} (耗时: {text_test.get('api_time', 'N/A')})")
                if text_test["api_status"] == "成功":
                    report.append(f"  - 响应长度: {text_test.get('response_length', 0)} 字符")
            
            report.append(f"- **总耗时**: {text_test.get('total_time', 'N/A')}")
        
        report.append("")
        
        # 带图片测试结果
        report.append("### 🖼️ 带图片测试")
        image_test = results.get("image_test", {})
        
        if "error" in image_test:
            report.append(f"- ❌ 测试失败: {image_test['error']}")
        elif image_test.get("status") == "测试图片不存在":
            report.append("- ⚠️ 测试图片不存在")
        else:
            # 图片压缩
            if "compress_status" in image_test:
                status = "✅" if image_test["compress_status"] == "成功" else "❌"
                report.append(f"- {status} 图片压缩: {image_test.get('original_size', 'N/A')} → {image_test.get('compressed_size', 'N/A')} (耗时: {image_test.get('compress_time', 'N/A')})")
            
            # 图片描述
            if "describe_status" in image_test:
                status = "✅" if image_test["describe_status"] == "成功" else "❌"
                report.append(f"- {status} 图片描述: {image_test['describe_status']} (耗时: {image_test.get('describe_time', 'N/A')})")
                if image_test["describe_status"] == "成功":
                    report.append(f"  - 描述长度: {image_test.get('description_length', 0)} 字符")
            
            # RAG检索
            if "rag_results" in image_test:
                report.append(f"- ✅ RAG检索: 找到 {image_test['rag_results']} 个相关文档 (耗时: {image_test.get('rag_time', 'N/A')})")
            
            report.append(f"- **总耗时**: {image_test.get('total_time', 'N/A')}")
        
        report.append("")
        
        # 系统状态
        report.append("### 📊 系统状态")
        system = results.get("system_status", {})
        
        if "error" in system:
            report.append(f"- ❌ 获取状态失败: {system['error']}")
        else:
            report.append(f"- RAG系统: {'已启用' if system.get('rag_enabled') else '未启用'}")
            if system.get("rag_enabled"):
                report.append(f"  - 文档总数: {system.get('total_chunks', 0)}")
                report.append(f"  - 向量模型: {system.get('embedding_model', 'unknown')}")
            
            report.append(f"- 并发任务: {system.get('current_tasks', 0)}/{system.get('max_parallel', 5)}")
            report.append(f"- 主模型: {system.get('main_model', '未配置')}")
            report.append(f"- 图片模型: {system.get('image_model', '未配置')}")
        
        report.append("")
        report.append(f"### ⏱️ 总测试时间: {results.get('total_time', 'N/A')}")
        
        return "\n".join(report)
    
    def _generate_test_summary(self, results: Dict) -> str:
        """生成测试摘要（用于嵌入消息）"""
        summary = []
        
        # 统计成功和失败的项目
        success_count = 0
        fail_count = 0
        
        # 检查文本测试
        text_test = results.get("text_test", {})
        if text_test.get("api_status") == "成功":
            success_count += 1
        else:
            fail_count += 1
        
        # 检查图片测试
        image_test = results.get("image_test", {})
        if image_test.get("describe_status") == "成功":
            success_count += 1
        else:
            fail_count += 1
        
        # 生成摘要
        if fail_count == 0:
            summary.append("✅ **所有测试通过！**")
        elif success_count > 0:
            summary.append(f"⚠️ **部分测试通过** ({success_count}/{success_count + fail_count})")
        else:
            summary.append("❌ **所有测试失败**")
        
        summary.append("")
        summary.append(f"📝 纯文本测试: {text_test.get('api_status', '未执行')}")
        summary.append(f"🖼️ 图片测试: {image_test.get('describe_status', '未执行')}")
        summary.append(f"⏱️ 总耗时: {results.get('total_time', 'N/A')}")
        summary.append("")
        summary.append("*详细报告已保存为附件*")
        
        return "\n".join(summary)

async def setup(bot: commands.Bot):
    """安装Cog"""
    await bot.add_cog(QuickDebug(bot))
    print("✅ QuickDebug cog 已加载")