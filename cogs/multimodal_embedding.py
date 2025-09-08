"""
多模态Embedding处理器
支持文本和图片的向量化处理
"""

import os
import io
import base64
import asyncio
from typing import List, Union, Dict, Optional, Tuple
import openai
from PIL import Image
import aiofiles
import hashlib
from enum import Enum


class ContentType(Enum):
    """内容类型枚举"""
    TEXT = "text"
    IMAGE = "image"
    MIXED = "mixed"


class MultimodalEmbeddingHandler:
    """多模态Embedding处理器"""
    
    def __init__(self, client: openai.OpenAI, model: str = "gemini-embedding-exp-03-07"):
        """
        初始化多模态Embedding处理器
        
        Args:
            client: OpenAI客户端实例
            model: Embedding模型名称
        """
        self.client = client
        self.model = model
        self.max_image_size = (1024, 1024)  # 默认最大图片尺寸
        
    async def get_embedding(
        self,
        content: Union[str, bytes],
        content_type: Optional[ContentType] = None
    ) -> List[float]:
        """
        获取内容的embedding向量
        
        Args:
            content: 文本字符串或图片字节数据
            content_type: 内容类型，如果为None则自动检测
            
        Returns:
            embedding向量
        """
        if content_type is None:
            content_type = self._detect_content_type(content)
            
        if content_type == ContentType.TEXT:
            return await self._get_text_embedding(content)
        elif content_type == ContentType.IMAGE:
            return await self._get_image_embedding(content)
        else:
            raise ValueError(f"不支持的内容类型: {content_type}")
            
    async def get_multimodal_embedding(
        self,
        text: Optional[str] = None,
        image: Optional[bytes] = None,
        mode: str = "hybrid"
    ) -> Tuple[List[float], Dict[str, any]]:
        """
        获取多模态内容的embedding向量
        
        Args:
            text: 文本内容
            image: 图片字节数据
            mode: 处理模式 - "text_only", "image_only", "hybrid"
            
        Returns:
            (embedding向量, 元数据)
        """
        metadata = {"mode": mode}
        
        if mode == "hybrid" and text and image:
            # 对于支持多模态的模型，可以直接将文本和图片一起发送
            # 这里我们使用组合方式
            text_embedding = await self._get_text_embedding(text)
            image_embedding = await self._get_image_embedding(image)
            
            # 检查embedding维度
            print(f"🔍 [多模态] 文本embedding维度: {len(text_embedding)}")
            print(f"🔍 [多模态] 图片embedding维度: {len(image_embedding)}")
            
            # 简单的平均组合（可以根据需要调整权重）
            combined_embedding = [
                (t + i) / 2 for t, i in zip(text_embedding, image_embedding)
            ]
            print(f"🔍 [多模态] 组合后embedding维度: {len(combined_embedding)}")
            
            metadata.update({
                "has_text": True,
                "has_image": True,
                "combination_method": "average"
            })
            
            return combined_embedding, metadata
            
        elif image and mode != "text_only":
            embedding = await self._get_image_embedding(image)
            metadata.update({"has_text": False, "has_image": True})
            return embedding, metadata
            
        elif text:
            embedding = await self._get_text_embedding(text)
            metadata.update({"has_text": True, "has_image": False})
            return embedding, metadata
            
        else:
            raise ValueError("必须提供至少一种内容（文本或图片）")
            
    async def _get_text_embedding(self, text: str) -> List[float]:
        """获取文本的embedding"""
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self.client.embeddings.create(
                model=self.model,
                input=text
            )
        )
        return response.data[0].embedding
        
    async def _get_image_embedding(self, image_data: bytes) -> List[float]:
        """
        获取图片的embedding
        
        对于支持多模态的gemini-embedding模型，我们将图片转换为base64格式
        注意：根据测试，该API只接受data URI格式的图片输入
        """
        import time
        start_time = time.time()
        
        # 预处理图片
        print(f"🖼️ [多模态] 开始预处理图片，原始大小: {len(image_data)} bytes")
        processed_image = await self._preprocess_image(image_data)
        print(f"🖼️ [多模态] 图片预处理完成，处理后大小: {len(processed_image)} bytes")
        
        # 转换为base64
        image_base64 = base64.b64encode(processed_image).decode('utf-8')
        print(f"🖼️ [多模态] Base64编码完成，编码后长度: {len(image_base64)} chars")
        
        # 获取embedding
        loop = asyncio.get_event_loop()
        
        try:
            # 直接使用data URI格式，这是唯一有效的方式
            print(f"🖼️ [多模态] 调用embedding API")
            print(f"   - 模型: {self.model}")
            print(f"   - API base URL: {self.client.base_url}")
            print(f"   - 输入格式: data URI")
            
            response = await loop.run_in_executor(
                None,
                lambda: self.client.embeddings.create(
                    model=self.model,
                    input=f"data:image/jpeg;base64,{image_base64}"
                )
            )
            
            duration = time.time() - start_time
            print(f"✅ [多模态] 成功获取图片embedding! 耗时: {duration:.2f}秒")
            return response.data[0].embedding
            
        except Exception as e:
            print(f"❌ [多模态] 获取图片embedding失败: {type(e).__name__}: {str(e)}")
            
            # 如果是500错误，提供更详细的错误信息
            if "500" in str(e) or "InternalServerError" in str(e):
                print(f"💡 [多模态] 提示：API返回500错误，可能是服务端问题或格式不支持")
                print(f"   - 图片大小: {len(processed_image)} bytes")
                print(f"   - Base64长度: {len(image_base64)} chars")
            
            raise e
                
    async def _preprocess_image(self, image_data: bytes) -> bytes:
        """
        预处理图片：调整大小、转换格式等
        
        Args:
            image_data: 原始图片字节数据
            
        Returns:
            处理后的图片字节数据
        """
        # 在异步上下文中处理图片
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._sync_preprocess_image, image_data)
        print(f"🖼️ [多模态] 图片预处理: 原始 {len(image_data)} bytes -> 处理后 {len(result)} bytes")
        return result
        
    def _sync_preprocess_image(self, image_data: bytes) -> bytes:
        """同步版本的图片预处理"""
        # 打开图片
        image = Image.open(io.BytesIO(image_data))
        
        # 转换为RGB（如果需要）
        if image.mode not in ('RGB', 'L'):
            image = image.convert('RGB')
            
        # 调整大小（如果超过最大尺寸）
        if image.size[0] > self.max_image_size[0] or image.size[1] > self.max_image_size[1]:
            image.thumbnail(self.max_image_size, Image.Resampling.LANCZOS)
            
        # 保存为JPEG格式
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=85)
        return output.getvalue()
        
    def _detect_content_type(self, content: Union[str, bytes]) -> ContentType:
        """自动检测内容类型"""
        if isinstance(content, str):
            return ContentType.TEXT
        elif isinstance(content, bytes):
            return ContentType.IMAGE
        else:
            raise ValueError(f"无法识别的内容类型: {type(content)}")
            
    async def batch_get_embeddings(
        self,
        contents: List[Union[str, bytes]],
        content_types: Optional[List[ContentType]] = None
    ) -> List[List[float]]:
        """
        批量获取embeddings
        
        Args:
            contents: 内容列表
            content_types: 内容类型列表，如果为None则自动检测
            
        Returns:
            embedding向量列表
        """
        if content_types is None:
            content_types = [self._detect_content_type(c) for c in contents]
            
        # 分离文本和图片
        text_indices = []
        text_contents = []
        image_indices = []
        image_contents = []
        
        for i, (content, ctype) in enumerate(zip(contents, content_types)):
            if ctype == ContentType.TEXT:
                text_indices.append(i)
                text_contents.append(content)
            elif ctype == ContentType.IMAGE:
                image_indices.append(i)
                image_contents.append(content)
                
        # 批量处理文本
        text_embeddings = []
        if text_contents:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.client.embeddings.create(
                    model=self.model,
                    input=text_contents
                )
            )
            text_embeddings = [item.embedding for item in response.data]
            
        # 处理图片（目前需要逐个处理）
        image_embeddings = []
        for image_data in image_contents:
            embedding = await self._get_image_embedding(image_data)
            image_embeddings.append(embedding)
            
        # 按原始顺序组合结果
        results = [None] * len(contents)
        
        for idx, embedding in zip(text_indices, text_embeddings):
            results[idx] = embedding
            
        for idx, embedding in zip(image_indices, image_embeddings):
            results[idx] = embedding
            
        return results


class MultimodalDocument:
    """多模态文档类，用于表示包含文本和图片的文档"""
    
    def __init__(
        self,
        doc_id: Optional[str] = None,
        text: Optional[str] = None,
        images: Optional[List[bytes]] = None,
        metadata: Optional[Dict] = None
    ):
        """
        初始化多模态文档
        
        Args:
            doc_id: 文档ID，如果为None则自动生成
            text: 文本内容
            images: 图片列表（字节数据）
            metadata: 额外的元数据
        """
        self.doc_id = doc_id or self._generate_id(text, images)
        self.text = text
        self.images = images or []
        self.metadata = metadata or {}
        
    def _generate_id(self, text: Optional[str], images: Optional[List[bytes]]) -> str:
        """基于内容生成唯一ID"""
        hasher = hashlib.sha256()
        
        if text:
            hasher.update(text.encode('utf-8'))
            
        if images:
            for img in images:
                hasher.update(img)
                
        return hasher.hexdigest()[:16]
        
    def has_text(self) -> bool:
        """是否包含文本"""
        return bool(self.text and self.text.strip())
        
    def has_images(self) -> bool:
        """是否包含图片"""
        return bool(self.images)
        
    def is_multimodal(self) -> bool:
        """是否为多模态文档（同时包含文本和图片）"""
        return self.has_text() and self.has_images()
        
    def get_content_type(self) -> ContentType:
        """获取内容类型"""
        if self.is_multimodal():
            return ContentType.MIXED
        elif self.has_images():
            return ContentType.IMAGE
        elif self.has_text():
            return ContentType.TEXT
        else:
            raise ValueError("文档不包含任何内容")
            
    async def save_images(self, directory: str) -> List[str]:
        """
        保存图片到指定目录
        
        Args:
            directory: 目标目录
            
        Returns:
            保存的图片路径列表
        """
        os.makedirs(directory, exist_ok=True)
        paths = []
        
        for i, image_data in enumerate(self.images):
            filename = f"{self.doc_id}_image_{i}.jpg"
            filepath = os.path.join(directory, filename)
            
            async with aiofiles.open(filepath, 'wb') as f:
                await f.write(image_data)
                
            paths.append(filepath)
            
        return paths
        
    def to_dict(self) -> Dict:
        """转换为字典格式"""
        return {
            "doc_id": self.doc_id,
            "text": self.text,
            "has_images": self.has_images(),
            "image_count": len(self.images),
            "content_type": self.get_content_type().value,
            "metadata": self.metadata
        }


# 辅助函数
def encode_image_to_base64(image_path: str) -> str:
    """将图片文件编码为Base64字符串"""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')
        

async def load_image_as_bytes(image_path: str) -> bytes:
    """异步加载图片文件为字节数据"""
    async with aiofiles.open(image_path, 'rb') as f:
        return await f.read()


# Discord bot setup函数
async def setup(bot):
    """
    Discord扩展的setup函数
    这个模块只提供工具类，不注册任何cog
    """
    # 这个模块不需要注册任何cog，只是提供工具类
    # 其他cog（如rag_processor）会导入并使用这些类
    pass