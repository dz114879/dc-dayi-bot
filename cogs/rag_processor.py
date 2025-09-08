"""  
RAG处理器模块
负责文档向量化、检索和上下文构建
支持多模态内容（文本和图片）
"""

import os
import asyncio
import json
from typing import List, Dict, Optional, Tuple, Union
import openai
import tiktoken
import chromadb
from chromadb.config import Settings
from langchain.text_splitter import RecursiveCharacterTextSplitter
import hashlib
import time
import discord
from discord.ext import commands
from cogs.rag_indexer import RAGIndexer
from cogs.multimodal_embedding import (
    MultimodalEmbeddingHandler,
    MultimodalDocument,
    ContentType,
    load_image_as_bytes
)


class RAGProcessor:
    """RAG处理器主类"""
    
    def __init__(self, db_path: str = "./rag_data/chroma_db"):
        """
        初始化RAG处理器
        
        Args:
            db_path: ChromaDB数据库路径
        """
        self.db_path = db_path
        
        # 从环境变量读取配置
        self.chunk_size = int(os.getenv("RAG_CHUNK_SIZE", "500"))  # 从环境变量读取分块大小（tokens）
        self.chunk_overlap = int(os.getenv("RAG_CHUNK_OVERLAP", "50"))  # 从环境变量读取重叠大小（tokens）
        self.top_k = int(os.getenv("RAG_TOP_K", "5"))
        self.min_similarity = float(os.getenv("RAG_MIN_SIMILARITY", "0.25"))
        self.embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
        
        # 批量处理配置
        self.max_batch_tokens = 10000  # 留一些余量，API限制是20k
        self.api_rate_limit = 50  # 每分钟20次请求
        self.last_api_call_times = []  # 记录最近的API调用时间
        
        # 多模态配置
        self.multimodal_enabled = os.getenv("MULTIMODAL_RAG_ENABLED", "true").lower() == "true"
        self.image_storage_path = os.getenv("IMAGE_STORAGE_PATH", "./rag_data/images")
        self.multimodal_search_mode = os.getenv("MULTIMODAL_SEARCH_MODE", "hybrid")
        
        # 初始化向量数据库
        self._init_vector_db()
        
        # 初始化embedding客户端
        self._init_embedding_client()
        
        # 初始化文本分割器
        self._init_text_splitter()
        self.indexer = RAGIndexer(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)
        
        # 初始化多模态处理器
        if self.multimodal_enabled:
            self.multimodal_handler = MultimodalEmbeddingHandler(
                client=self.embedding_client,
                model=self.embedding_model
            )
            # 确保图片存储目录存在
            os.makedirs(self.image_storage_path, exist_ok=True)
        
        # 加载提示词模板
        self._load_prompt_templates()
        
    def _init_vector_db(self):
        """初始化ChromaDB向量数据库"""
        try:
            # 确保数据库目录存在
            os.makedirs(self.db_path, exist_ok=True)
            
            # 创建ChromaDB客户端
            self.chroma_client = chromadb.PersistentClient(
                path=self.db_path,
                settings=Settings(
                    anonymized_telemetry=False,
                    allow_reset=True
                )
            )
            
            # 获取或创建集合
            self.collection = self.chroma_client.get_or_create_collection(
                name="knowledge_base",
                metadata={"description": "答疑机器人知识库"}
            )
            
            print(f"✅ 向量数据库初始化成功: {self.db_path}")
            
        except Exception as e:
            print(f"❌ 向量数据库初始化失败: {e}")
            raise
            
    def _init_embedding_client(self):
        """初始化Embedding API客户端"""
        # 优先使用专门的embedding配置，如果没有则使用通用OpenAI配置
        api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
        api_base = os.getenv("EMBEDDING_API_BASE") or os.getenv("OPENAI_API_BASE_URL")
        
        # 添加调试日志
        print(f"🔧 [RAG] 初始化Embedding客户端:")
        print(f"   - EMBEDDING_API_KEY: {'已设置' if os.getenv('EMBEDDING_API_KEY') else '未设置'}")
        print(f"   - EMBEDDING_API_BASE: {os.getenv('EMBEDDING_API_BASE') or '未设置'}")
        print(f"   - 实际使用的API Key: {'EMBEDDING_API_KEY' if os.getenv('EMBEDDING_API_KEY') else 'OPENAI_API_KEY'}")
        print(f"   - 实际使用的Base URL: {api_base}")
        
        if not api_key:
            raise ValueError("未配置EMBEDDING_API_KEY或OPENAI_API_KEY")
        
        if not api_base:
            print("⚠️ [RAG] 警告：base_url为空，这会导致连接错误！")
            
        self.embedding_client = openai.OpenAI(
            api_key=api_key,
            base_url=api_base
        )
        print(f"✅ [RAG] Embedding客户端初始化完成")
        
    def _init_text_splitter(self):
        """初始化文本分割器"""
        # 使用tiktoken计算token数量
        self.encoding = tiktoken.get_encoding("cl100k_base")
        
        # 创建递归字符分割器
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size * 4,  # 粗略估计：1 token ≈ 4 characters
            chunk_overlap=self.chunk_overlap * 4,
            length_function=self._count_tokens,
            separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""]
        )
        
    def _count_tokens(self, text: str) -> int:
        """计算文本的token数量"""
        return len(self.encoding.encode(text))
        
    def _load_prompt_templates(self):
        """加载提示词模板文件"""
        try:
            # 加载系统提示词（头部）
            app_head_path = "./rag_prompt/app_head.txt"
            if os.path.exists(app_head_path):
                with open(app_head_path, 'r', encoding='utf-8') as f:
                    self.prompt_head = f.read().strip()
            else:
                print(f"⚠️ 未找到系统提示词文件 {app_head_path}，使用默认值")
                self.prompt_head = "基于以下相关知识回答用户问题："
                
            # 加载兜底提示词（尾部）
            app_end_path = "./rag_prompt/app_end.txt"
            if os.path.exists(app_end_path):
                with open(app_end_path, 'r', encoding='utf-8') as f:
                    self.prompt_end = f.read().strip()
            else:
                print(f"⚠️ 未找到兜底提示词文件 {app_end_path}，使用默认值")
                self.prompt_end = "请根据提供的相关知识准确回答用户问题。如果相关知识不足以回答问题，请诚实地说明。"
                
            print(f"✅ 提示词模板加载成功")
            
        except Exception as e:
            print(f"❌ 加载提示词模板失败: {e}")
            # 使用默认值
            self.prompt_head = "基于以下相关知识回答用户问题："
            self.prompt_end = "请根据提供的相关知识准确回答用户问题。如果相关知识不足以回答问题，请诚实地说明。"
        
    async def _wait_for_rate_limit(self):
        """等待以遵守API速率限制"""
        now = time.time()
        # 清理超过60秒的记录
        self.last_api_call_times = [t for t in self.last_api_call_times if now - t < 60]
        
        # 如果过去60秒内的请求数达到限制，等待
        if len(self.last_api_call_times) >= self.api_rate_limit:
            wait_time = 60 - (now - self.last_api_call_times[0]) + 1  # 额外等待1秒
            if wait_time > 0:
                print(f"⏳ 达到API速率限制，等待 {wait_time:.1f} 秒...")
                await asyncio.sleep(wait_time)
                # 重新计算
                now = time.time()
                self.last_api_call_times = [t for t in self.last_api_call_times if now - t < 60]
        
        # 记录本次调用时间
        self.last_api_call_times.append(now)
    
    async def get_embedding(self, content: Union[str, bytes], content_type: Optional[str] = None) -> List[float]:
        """
        获取内容的向量表示（支持文本和图片）
        
        Args:
            content: 要向量化的内容（文本字符串或图片字节）
            content_type: 内容类型（"text"或"image"），如果为None则自动检测
            
        Returns:
            向量列表
        """
        if self.multimodal_enabled and isinstance(content, bytes):
            # 使用多模态处理器处理图片
            return await self.multimodal_handler.get_embedding(
                content,
                ContentType.IMAGE if content_type == "image" else None
            )
        elif isinstance(content, str):
            # 处理文本
            return (await self.get_embeddings_batch([content]))[0]
        else:
            raise ValueError(f"不支持的内容类型: {type(content)}")
    
    async def get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """
        批量获取文本的向量表示
        
        Args:
            texts: 要向量化的文本列表
            
        Returns:
            向量列表的列表
        """
        try:
            # 等待速率限制
            await self._wait_for_rate_limit()
            
            # 调试日志
            print(f"🔄 [RAG] 正在调用embedding API:")
            print(f"   - 模型: {self.embedding_model}")
            print(f"   - 文本数量: {len(texts)}")
            print(f"   - Base URL: {self.embedding_client.base_url}")
            
            # 使用asyncio运行同步的OpenAI API调用
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.embedding_client.embeddings.create(
                    model=self.embedding_model,
                    input=texts
                )
            )
            
            # 按照输入顺序返回embeddings
            return [item.embedding for item in response.data]
            
        except Exception as e:
            if "429" in str(e):
                print(f"⚠️ 遇到429错误，等待60秒后重试...")
                await asyncio.sleep(60)
                # 递归重试
                return await self.get_embeddings_batch(texts)
            else:
                print(f"❌ 批量获取embedding失败: {e}")
                raise
            
    def split_text(self, text: str, metadata: Optional[Dict] = None) -> List[Dict]:
        """
        使用RAGIndexer进行智能分块
        
        Args:
            text: 要分块的文本
            metadata: 额外的元数据
            
        Returns:
            包含文本块和元数据的字典列表
        """
        chunks = self.indexer.smart_split(text, metadata)
        
        # 为每个块生成唯一的ID并格式化
        result = []
        for i, chunk_data in enumerate(chunks):
            chunk_text = chunk_data["text"]
            chunk_metadata = chunk_data["metadata"]
            
            # 更新元数据
            chunk_metadata["chunk_index"] = i
            chunk_metadata["chunk_total"] = len(chunks)
            
            # 清理metadata，确保ChromaDB兼容性（只支持str, int, float, bool, None）
            cleaned_metadata = {}
            for key, value in chunk_metadata.items():
                if isinstance(value, list):
                    # 将列表转换为逗号分隔的字符串
                    if value:  # 非空列表
                        if isinstance(value[0], tuple):
                            # 处理parent_titles这样的元组列表
                            cleaned_metadata[key] = ",".join([str(item[1]) if len(item) > 1 else str(item[0]) for item in value])
                        else:
                            cleaned_metadata[key] = ",".join([str(item) for item in value])
                    else:
                        cleaned_metadata[key] = ""
                elif isinstance(value, (str, int, float, bool)) or value is None:
                    cleaned_metadata[key] = value
                else:
                    # 其他类型转换为字符串
                    cleaned_metadata[key] = str(value)
            
            chunk_metadata = cleaned_metadata
            
            # 生成块的唯一ID - 包含源文件、索引和时间戳确保唯一性
            source_info = chunk_metadata.get("source", "unknown")
            timestamp = str(int(time.time() * 1000))  # 毫秒级时间戳
            unique_string = f"{source_info}_{i}_{timestamp}_{chunk_text[:100]}"
            chunk_id = hashlib.md5(unique_string.encode()).hexdigest()
            
            result.append({
                "id": chunk_id,
                "text": chunk_text,
                "metadata": chunk_metadata
            })
            
        return result
        
    async def index_document(self, text: str, source: str = "unknown") -> int:
        """
        索引文档到向量数据库
        
        Args:
            text: 文档文本
            source: 文档来源
            
        Returns:
            索引的块数量
        """
        try:
            # 分块文档
            chunks = self.split_text(text, metadata={"source": source, "content_type": "text"})
            
            if not chunks:
                print("⚠️ 没有生成任何文本块")
                return 0
                
            print(f"📝 正在索引 {len(chunks)} 个文本块...")
            
            # 将chunks分组为批次，确保每批的总token数不超过限制
            batches = []
            current_batch = []
            current_tokens = 0
            
            for chunk in chunks:
                chunk_tokens = chunk["metadata"]["tokens"]
                
                # 如果当前批次加上这个chunk会超过限制，则开始新批次
                if current_tokens + chunk_tokens > self.max_batch_tokens and current_batch:
                    batches.append(current_batch)
                    current_batch = [chunk]
                    current_tokens = chunk_tokens
                else:
                    current_batch.append(chunk)
                    current_tokens += chunk_tokens
            
            # 添加最后一个批次
            if current_batch:
                batches.append(current_batch)
            
            print(f"📦 分为 {len(batches)} 个批次进行处理")
            
            # 处理每个批次
            all_ids = []
            all_texts = []
            all_embeddings = []
            all_metadatas = []
            
            for batch_idx, batch in enumerate(batches, 1):
                print(f"  处理批次 {batch_idx}/{len(batches)}：{len(batch)} 个文本块，"
                      f"约 {sum(c['metadata']['tokens'] for c in batch)} tokens")
                
                # 批量获取embeddings
                batch_texts = [chunk["text"] for chunk in batch]
                batch_embeddings = await self.get_embeddings_batch(batch_texts)
                
                # 收集数据
                for chunk, embedding in zip(batch, batch_embeddings):
                    all_ids.append(chunk["id"])
                    all_texts.append(chunk["text"])
                    all_embeddings.append(embedding)
                    all_metadatas.append(chunk["metadata"])
            
            # 一次性添加到向量数据库
            self.collection.add(
                ids=all_ids,
                documents=all_texts,
                embeddings=all_embeddings,
                metadatas=all_metadatas
            )
            
            print(f"✅ 成功索引 {len(chunks)} 个文本块")
            return len(chunks)
            
        except Exception as e:
            print(f"❌ 索引文档失败: {e}")
            raise
            
    async def index_image(
        self,
        image_data: bytes,
        source: str = "unknown",
        text_description: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> str:
        """
        索引图片到向量数据库
        
        Args:
            image_data: 图片字节数据
            source: 图片来源
            text_description: 图片的文本描述（可选）
            metadata: 额外的元数据
            
        Returns:
            图片ID
        """
        if not self.multimodal_enabled:
            raise ValueError("多模态功能未启用")
            
        try:
            # 生成图片ID
            image_id = hashlib.md5(image_data).hexdigest()[:16]
            
            # 保存图片到本地
            image_filename = f"{image_id}.jpg"
            image_path = os.path.join(self.image_storage_path, image_filename)
            
            # 预处理并保存图片
            processed_image = await self.multimodal_handler._preprocess_image(image_data)
            with open(image_path, 'wb') as f:
                f.write(processed_image)
            
            print(f"🖼️ 正在索引图片: {image_filename}")
            
            # 获取图片embedding
            image_embedding = await self.multimodal_handler.get_embedding(
                processed_image, ContentType.IMAGE
            )
            
            # 准备元数据
            image_metadata = {
                "source": source,
                "content_type": "image",
                "image_path": image_path,
                "image_filename": image_filename,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                **(metadata or {})
            }
            
            # 如果没有提供描述，使用默认描述
            if not text_description:
                text_description = f"Image from {source}"
            
            # 添加到向量数据库
            self.collection.add(
                ids=[image_id],
                documents=[text_description],
                embeddings=[image_embedding],
                metadatas=[image_metadata]
            )
            
            print(f"✅ 成功索引图片: {image_id}")
            return image_id
            
        except Exception as e:
            print(f"❌ 索引图片失败: {e}")
            raise
            
    async def index_multimodal_document(
        self,
        document: MultimodalDocument,
        source: str = "unknown"
    ) -> Dict[str, any]:
        """
        索引多模态文档（包含文本和图片）
        
        Args:
            document: 多模态文档对象
            source: 文档来源
            
        Returns:
            索引结果统计
        """
        if not self.multimodal_enabled:
            # 如果多模态未启用，只索引文本部分
            if document.has_text():
                chunks_count = await self.index_document(document.text, source)
                return {"text_chunks": chunks_count, "images": 0}
            else:
                return {"text_chunks": 0, "images": 0}
                
        try:
            results = {"text_chunks": 0, "images": 0, "mixed_chunks": 0}
            
            # 保存图片
            image_paths = []
            if document.has_images():
                image_paths = await document.save_images(self.image_storage_path)
                
            # 如果是纯文本文档
            if document.has_text() and not document.has_images():
                results["text_chunks"] = await self.index_document(document.text, source)
                
            # 如果是纯图片文档
            elif document.has_images() and not document.has_text():
                for i, (image_data, image_path) in enumerate(zip(document.images, image_paths)):
                    await self.index_image(
                        image_data,
                        source,
                        f"Image {i+1} from document {document.doc_id}",
                        {"document_id": document.doc_id, "image_index": i}
                    )
                    results["images"] += 1
                    
            # 如果是混合文档
            elif document.is_multimodal():
                # 分块文本，但添加图片引用信息
                chunks = self.split_text(
                    document.text,
                    metadata={
                        "source": source,
                        "content_type": "mixed",
                        "document_id": document.doc_id,
                        "has_images": True,
                        "image_count": len(document.images)
                    }
                )
                
                # 索引文本块
                for chunk in chunks:
                    # ChromaDB不支持列表类型的metadata，需要转换为字符串
                    if image_paths:
                        # 将图片路径列表转换为逗号分隔的字符串
                        chunk["metadata"]["associated_images"] = ",".join(image_paths)
                        chunk["metadata"]["associated_images_count"] = len(image_paths)
                    
                # 批量处理文本块（复用现有逻辑）
                if chunks:
                    # 获取embeddings并添加到数据库
                    texts = [chunk["text"] for chunk in chunks]
                    embeddings = await self.get_embeddings_batch(texts)
                    
                    self.collection.add(
                        ids=[chunk["id"] for chunk in chunks],
                        documents=texts,
                        embeddings=embeddings,
                        metadatas=[chunk["metadata"] for chunk in chunks]
                    )
                    
                    results["text_chunks"] = len(chunks)
                    
                # 同时索引图片，关联到文档
                for i, (image_data, image_path) in enumerate(zip(document.images, image_paths)):
                    await self.index_image(
                        image_data,
                        source,
                        f"Image {i+1} associated with text: {document.text[:100]}...",
                        {
                            "document_id": document.doc_id,
                            "image_index": i,
                            "associated_text": document.text[:200]
                        }
                    )
                    results["images"] += 1
                    
                results["mixed_chunks"] = results["text_chunks"]
                
            print(f"✅ 成功索引多模态文档: {results}")
            return results
            
        except Exception as e:
            print(f"❌ 索引多模态文档失败: {e}")
            raise
            
    async def retrieve_context(
        self,
        query: Union[str, bytes, Dict],
        top_k: Optional[int] = None,
        mode: Optional[str] = None
    ) -> List[Dict]:
        """
        检索相关上下文（支持多模态查询）
        
        Args:
            query: 查询内容 - 可以是文本、图片字节或包含text/image键的字典
            top_k: 返回的文档数量
            mode: 检索模式 - "text_only", "image_only", "hybrid"
            
        Returns:
            相关文档列表
        """
        try:
            # 使用配置的top_k或默认值
            k = top_k or self.top_k
            search_mode = mode or self.multimodal_search_mode
            
            # 获取查询的embedding
            if isinstance(query, dict) and self.multimodal_enabled:
                # 多模态查询
                query_embedding, metadata = await self.multimodal_handler.get_multimodal_embedding(
                    text=query.get("text"),
                    image=query.get("image"),
                    mode=search_mode
                )
            elif isinstance(query, bytes) and self.multimodal_enabled:
                # 图片查询
                query_embedding = await self.multimodal_handler.get_embedding(
                    query, ContentType.IMAGE
                )
            else:
                # 文本查询（向后兼容）
                if isinstance(query, dict):
                    query = query.get("text", "")
                query_embeddings = await self.get_embeddings_batch([query])
                query_embedding = query_embeddings[0]
            
            # 向量相似度搜索
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=k,
                include=["documents", "metadatas", "distances"]
            )
            
            # 处理结果
            contexts = []
            if results["documents"] and len(results["documents"]) > 0:
                for i, doc in enumerate(results["documents"][0]):
                    # 计算相似度（ChromaDB返回的是距离，需要转换）
                    distance = results["distances"][0][i] if results["distances"] else 0
                    similarity = 1 - distance  # 简单的相似度计算
                    
                    # 过滤低相似度的结果
                    if similarity >= self.min_similarity:
                        metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                        context = {
                            "text": doc,
                            "metadata": metadata,
                            "similarity": similarity
                        }
                        
                        # 如果是图片结果，添加图片路径信息
                        if metadata.get("content_type") == "image" and metadata.get("image_path"):
                            context["image_path"] = metadata["image_path"]
                            
                        contexts.append(context)
                        
            # 按相似度排序
            contexts.sort(key=lambda x: x["similarity"], reverse=True)
            
            return contexts
            
        except Exception as e:
            print(f"❌ 检索上下文失败: {e}")
            return []
            
    async def build_enhanced_prompt(self, query: Union[str, Dict], contexts: List[Dict]) -> str:
        """
        构建增强的提示词（支持多模态上下文）
        
        Args:
            query: 用户查询（文本或包含text/image的字典）
            contexts: 检索到的上下文
            
        Returns:
            增强后的提示词
        """
        if not contexts:
            # 如果没有检索到相关内容，返回原始查询
            if isinstance(query, dict):
                return query.get("text", "")
            return query
            
        # 提取查询文本
        query_text = query if isinstance(query, str) else query.get("text", "")
        
        # 构建上下文部分
        context_parts = []
        image_references = []
        
        for i, ctx in enumerate(contexts, 1):
            # 处理文本上下文
            context_parts.append(f"[相关知识 {i}]\n{ctx['text']}\n")
            
            # 如果有关联的图片，添加引用
            if ctx.get("image_path"):
                image_references.append(f"[图片 {i}]: {ctx['image_path']}")
            elif ctx["metadata"].get("associated_images"):
                # associated_images 现在是逗号分隔的字符串
                img_paths = ctx["metadata"]["associated_images"].split(",")
                for img_path in img_paths:
                    image_references.append(f"[关联图片]: {img_path.strip()}")
                    
        # 构建图片引用部分
        image_section = ""
        if image_references:
            image_section = "\n[相关图片资源]\n" + "\n".join(image_references) + "\n"
            
        # 组合增强提示词，使用从文件加载的模板
        # 注意：不在系统提示词中包含用户问题，用户问题应该在user角色的消息中
        enhanced_prompt = f"""{self.prompt_head}

[知识库开始]
{''.join(context_parts)}
{image_section}
{self.prompt_end}"""
        
        return enhanced_prompt
        
    def get_stats(self) -> Dict:
        """
        获取RAG系统统计信息
        
        Returns:
            统计信息字典
        """
        try:
            # 获取集合统计
            count = self.collection.count()
            
            stats = {
                "status": "active",
                "database_path": self.db_path,
                "total_chunks": count,
                "embedding_model": self.embedding_model,
                "chunk_size": self.chunk_size,
                "chunk_overlap": self.chunk_overlap,
                "top_k": self.top_k,
                "min_similarity": self.min_similarity,
                "max_batch_tokens": self.max_batch_tokens,
                "api_rate_limit": f"{self.api_rate_limit} requests/min",
                "recent_api_calls": len(self.last_api_call_times)
            }
            
            # 添加多模态相关统计
            if self.multimodal_enabled:
                stats.update({
                    "multimodal_enabled": True,
                    "image_storage_path": self.image_storage_path,
                    "multimodal_search_mode": self.multimodal_search_mode
                })
                
                # 统计图片数量
                if os.path.exists(self.image_storage_path):
                    image_count = len([f for f in os.listdir(self.image_storage_path)
                                     if f.endswith(('.jpg', '.jpeg', '.png', '.gif'))])
                    stats["stored_images"] = image_count
                    
            return stats
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
            
    def clear_database(self):
        """清空向量数据库"""
        try:
            # 删除并重新创建集合
            self.chroma_client.delete_collection("knowledge_base")
            self.collection = self.chroma_client.create_collection(
                name="knowledge_base",
                metadata={"description": "答疑机器人知识库"}
            )
            print("✅ 向量数据库已清空")
        except Exception as e:
            print(f"❌ 清空数据库失败: {e}")
            raise


# 简单的文档分块工具函数
def simple_chunk_text(text: str, max_tokens: int = 500, overlap: int = 50) -> List[str]:
    """
    简单的文档分块功能
    
    Args:
        text: 要分块的文本
        max_tokens: 每块的最大token数
        overlap: 块之间的重叠token数
        
    Returns:
        文本块列表
    """
    # 使用tiktoken编码
    encoding = tiktoken.get_encoding("cl100k_base")
    
    # 将文本编码为tokens
    tokens = encoding.encode(text)
    
    chunks = []
    start = 0
    
    while start < len(tokens):
        # 计算块的结束位置
        end = min(start + max_tokens, len(tokens))
        
        # 提取token块并解码回文本
        chunk_tokens = tokens[start:end]
        chunk_text = encoding.decode(chunk_tokens)
        chunks.append(chunk_text)
        
        # 移动到下一个块的开始位置（考虑重叠）
        start = end - overlap if end < len(tokens) else end
        
    return chunks


# 测试函数
async def test_rag_processor():
    """测试RAG处理器功能"""
    print("🧪 开始测试RAG处理器...")
    
    # 创建处理器实例
    processor = RAGProcessor()
    
    # 测试文本
    test_text = """
    # SillyTavern 安装指南
    
    ## Windows安装
    1. 下载最新版本的安装包
    2. 解压到任意目录
    3. 运行start.bat文件
    
    ## 常见错误
    
    ### ETIMEDOUT错误
    这个错误通常表示网络连接超时。解决方法：
    - 检查网络连接
    - 使用代理
    - 重试操作
    
    ### 429错误
    这是API速率限制错误。解决方法：
    - 降低请求频率
    - 等待一段时间再试
    """
    
    # 测试分块
    print("\n📄 测试文档分块...")
    chunks = processor.split_text(test_text)
    print(f"生成了 {len(chunks)} 个文本块")
    
    # 测试索引
    print("\n📥 测试文档索引...")
    chunk_count = await processor.index_document(test_text, source="test")
    print(f"索引了 {chunk_count} 个文本块")
    
    # 测试检索
    print("\n🔍 测试上下文检索...")
    contexts = await processor.retrieve_context("ETIMEDOUT错误怎么解决？")
    print(f"检索到 {len(contexts)} 个相关文档")
    
    if contexts:
        print(f"最相关的文档（相似度: {contexts[0]['similarity']:.2f}）:")
        print(contexts[0]['text'][:200] + "...")
    
    # 获取统计信息
    print("\n📊 系统统计信息:")
    stats = processor.get_stats()
    for key, value in stats.items():
        print(f"  {key}: {value}")
    
    print("\n✅ 测试完成！")


class RAGProcessorCog(commands.Cog):
    """RAG处理器Cog，提供文档向量化和检索功能"""
    
    def __init__(self, bot):
        self.bot = bot
        self.processor = RAGProcessor()
    
    @commands.command(name="test_processor")
    async def test_processor_command(self, ctx):
        """测试RAG处理器功能"""
        stats = self.processor.get_stats()
        await ctx.send(f"RAG处理器功能正常运行\n数据库统计: {stats}")

async def setup(bot):
    """设置函数，用于加载cog"""
    await bot.add_cog(RAGProcessorCog(bot))

if __name__ == "__main__":
    # 运行测试
    asyncio.run(test_rag_processor())