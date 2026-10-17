"""
SmartTourism · ChromaDB RAG 引擎
轻量 RAG 引擎，用于搜索本地文旅知识库

功能:
  - 从 skills/*/references/*.md 构建向量索引
  - 语义检索：query(query_text, top_k=5)
  - 优雅降级：无 chromadb 时返回空结果

数据目录: D:\SmartTourism\data\chroma_db\
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("rag_engine")
logger.setLevel(logging.DEBUG)

# ==================== 常量 ====================

# 项目根目录
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ChromaDB 持久化目录
CHROMA_DB_DIR = os.path.join(_PROJECT_DIR, 'data', 'chroma_db')

# Skills 目录（含 references/*.md）
SKILLS_DIR = os.path.join(_PROJECT_DIR, 'skills')

# 集合名称
COLLECTION_NAME = "tourism_knowledge"

# Embedding 模型（使用 ChromaDB 内置的 sentence-transformers）
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


# ==================== 文档加载 ====================

def _list_reference_files(skills_dir: str = SKILLS_DIR) -> List[Path]:
    """
    扫描 skills/ 目录下所有 references/*.md 文件

    Returns:
        List[Path]: Markdown 文件路径列表
    """
    if not os.path.isdir(skills_dir):
        return []

    md_files: List[Path] = []
    skills_path = Path(skills_dir)

    for md_file in skills_path.rglob("references/*.md"):
        if md_file.is_file():
            md_files.append(md_file)

    return md_files


def _split_documents(
    content: str,
    chunk_size: int = 500,
    chunk_overlap: int = 100,
    separator: str = "\n\n",
) -> List[str]:
    """
    将长文档切分成 chunks

    Args:
        content: 文档内容
        chunk_size: 每个 chunk 最大字符数
        chunk_overlap: chunk 之间的重叠字符数
        separator: 段落分隔符

    Returns:
        List[str]: chunk 列表
    """
    if len(content) <= chunk_size:
        return [content] if content.strip() else []

    # 按段落分隔
    paragraphs = content.split(separator)
    chunks: List[str] = []
    current_chunk: List[str] = []
    current_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        para_len = len(para)
        if current_len + para_len > chunk_size and current_chunk:
            # 当前 chunk 已满，保存并开启新 chunk
            chunks.append(separator.join(current_chunk))
            # 重叠：保留最后一个段落
            overlap_start = max(0, len(current_chunk) - 1)
            current_chunk = current_chunk[overlap_start:] if overlap_start < len(current_chunk) else []
            current_len = sum(len(p) for p in current_chunk)

        current_chunk.append(para)
        current_len += para_len

    if current_chunk:
        chunks.append(separator.join(current_chunk))

    return chunks


# ==================== ChromaDB 检查 ====================

_CHROMA_AVAILABLE: Optional[bool] = None


def _is_chroma_available() -> bool:
    """检测 chromadb 是否可用"""
    global _CHROMA_AVAILABLE
    if _CHROMA_AVAILABLE is not None:
        return _CHROMA_AVAILABLE

    try:
        import chromadb  # noqa: F401
        _CHROMA_AVAILABLE = True
    except ImportError:
        logger.warning("chromadb 未安装，RAG 引擎将返回空结果。安装: pip install chromadb")
        _CHROMA_AVAILABLE = False

    return _CHROMA_AVAILABLE


# ==================== 主接口 ====================

class RAGEngine:
    """
    轻量 RAG 引擎

    使用示例:
        engine = RAGEngine()
        engine.build_index()  # 首次使用需要构建索引
        results = engine.search("杭州有什么好玩的景点")
        for doc, score in results:
            print(f"[{score:.3f}] {doc[:100]}...")
    """

    def __init__(self, db_dir: str = CHROMA_DB_DIR):
        """
        Args:
            db_dir: ChromaDB 持久化目录
        """
        self.db_dir = db_dir
        self.collection = None
        self._initialized = False

    # ---- 索引构建 ----

    def build_index(
        self,
        skills_dir: str = SKILLS_DIR,
        force_rebuild: bool = False,
    ) -> Dict[str, Any]:
        """
        从 skills/*/references/*.md 构建向量索引

        Args:
            skills_dir: Skills 目录路径
            force_rebuild: 是否强制重建（删除旧数据）

        Returns:
            Dict: {"success": bool, "documents_indexed": int, "chunks_created": int, "error": str | None}
        """
        if not _is_chroma_available():
            return {
                "success": False,
                "documents_indexed": 0,
                "chunks_created": 0,
                "error": "chromadb 未安装。请运行: pip install chromadb",
            }

        try:
            import chromadb
            from chromadb.config import Settings

            # 如果强制重建，删除旧数据
            if force_rebuild and os.path.exists(self.db_dir):
                import shutil
                shutil.rmtree(self.db_dir, ignore_errors=True)
                logger.info(f"已删除旧索引: {self.db_dir}")

            os.makedirs(self.db_dir, exist_ok=True)

            # 创建 ChromaDB 客户端（持久化模式）
            client = chromadb.PersistentClient(
                path=self.db_dir,
                settings=Settings(anonymized_telemetry=False),
            )

            # 获取或创建集合
            try:
                self.collection = client.get_collection(COLLECTION_NAME)
                if force_rebuild:
                    client.delete_collection(COLLECTION_NAME)
                    self.collection = client.create_collection(COLLECTION_NAME)
                    logger.info("已重建集合")
            except Exception:
                self.collection = client.create_collection(COLLECTION_NAME)
                logger.info(f"已创建集合: {COLLECTION_NAME}")

            # 加载文档并切分
            md_files = _list_reference_files(skills_dir)
            if not md_files:
                logger.warning(f"未在 {skills_dir} 中找到 references/*.md 文件")
                return {
                    "success": True,
                    "documents_indexed": 0,
                    "chunks_created": 0,
                    "error": None,
                }

            all_chunks: List[str] = []
            all_metadatas: List[Dict[str, str]] = []
            all_ids: List[str] = []

            for md_file in md_files:
                try:
                    with open(md_file, 'r', encoding='utf-8') as f:
                        content = f.read()

                    # 获取相对路径作为来源标识
                    rel_path = str(md_file.relative_to(skills_dir))
                    # 提取 skill 名称
                    skill_name = rel_path.split(os.sep)[0]

                    chunks = _split_documents(content)
                    for i, chunk in enumerate(chunks):
                        chunk_id = f"{skill_name}_{md_file.stem}_{i}"
                        all_chunks.append(chunk)
                        all_metadatas.append({
                            "source": rel_path,
                            "skill": skill_name,
                            "filename": md_file.name,
                            "chunk_index": str(i),
                        })
                        all_ids.append(chunk_id)

                except Exception as e:
                    logger.warning(f"读取文件失败 {md_file}: {e}")
                    continue

            if not all_chunks:
                return {
                    "success": True,
                    "documents_indexed": len(md_files),
                    "chunks_created": 0,
                    "error": None,
                }

            # 分批添加（ChromaDB 单次 add 有限制）
            batch_size = 100
            for i in range(0, len(all_chunks), batch_size):
                batch_end = min(i + batch_size, len(all_chunks))
                self.collection.add(
                    documents=all_chunks[i:batch_end],
                    metadatas=all_metadatas[i:batch_end],
                    ids=all_ids[i:batch_end],
                )

            logger.info(
                f"索引构建完成: {len(md_files)} 个文档, "
                f"{len(all_chunks)} 个 chunk"
            )

            self._initialized = True
            return {
                "success": True,
                "documents_indexed": len(md_files),
                "chunks_created": len(all_chunks),
                "error": None,
            }

        except Exception as e:
            logger.exception("索引构建失败")
            return {
                "success": False,
                "documents_indexed": 0,
                "chunks_created": 0,
                "error": str(e),
            }

    # ---- 检索 ----

    def search(
        self,
        query_text: str,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """
        语义检索本地知识库

        Args:
            query_text: 查询文本
            top_k: 返回的文档数量
            min_score: 最低相似度阈值（0-1，低于此值的文档会被过滤）

        Returns:
            List[Tuple[str, float, Dict]]:
                每个元素为 (文档内容, 相似度分数, 元数据字典)
        """
        if not _is_chroma_available() or not self._ensure_collection():
            logger.debug("RAG 不可用，返回空结果")
            return []

        try:
            results = self.collection.query(
                query_texts=[query_text],
                n_results=top_k,
            )

            # ChromaDB 返回格式:
            # {
            #   "ids": [["id1", "id2", ...]],
            #   "documents": [["doc1", "doc2", ...]],
            #   "metadatas": [[{...}, {...}, ...]],
            #   "distances": [[0.1, 0.2, ...]]
            # }

            ids = results.get("ids", [[]])[0]
            documents = results.get("documents", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]
            distances = results.get("distances", [[]])[0]

            output: List[Tuple[str, float, Dict[str, Any]]] = []
            for i, doc in enumerate(documents):
                # 距离转相似度（cosine distance → similarity = 1 - distance）
                distance = distances[i] if i < len(distances) else 1.0
                similarity = 1.0 - distance if distance is not None else 0.0

                if similarity < min_score:
                    continue

                metadata = metadatas[i] if i < len(metadatas) else {}
                output.append((doc, similarity, metadata))

            return output

        except Exception as e:
            logger.exception(f"检索失败: {e}")
            return []

    def search_formatted(
        self,
        query_text: str,
        top_k: int = 5,
    ) -> str:
        """
        检索并格式化为 LLM 上下文

        Args:
            query_text: 查询文本
            top_k: 返回数量

        Returns:
            str: 格式化的上下文文本，可直接注入到 LLM prompt
        """
        results = self.search(query_text, top_k=top_k)
        if not results:
            return ""

        parts = ["以下是本地知识库中相关的文旅资料：\n"]
        for i, (doc, score, meta) in enumerate(results, 1):
            source = meta.get("source", "未知来源")
            parts.append(f"### 参考资料 {i} (相关度: {score:.1%}, 来源: {source})")
            parts.append(doc[:800])  # 限制单篇长度
            parts.append("")

        return "\n".join(parts)

    # ---- 辅助方法 ----

    def _ensure_collection(self) -> bool:
        """确保集合已加载"""
        if self.collection is not None:
            return True

        if not _is_chroma_available():
            return False

        try:
            import chromadb
            from chromadb.config import Settings

            client = chromadb.PersistentClient(
                path=self.db_dir,
                settings=Settings(anonymized_telemetry=False),
            )
            self.collection = client.get_collection(COLLECTION_NAME)
            self._initialized = True
            return True
        except Exception:
            return False

    def get_stats(self) -> Dict[str, Any]:
        """获取索引统计信息"""
        if not self._ensure_collection():
            return {"available": False, "error": "ChromaDB 不可用或索引未构建"}

        try:
            count = self.collection.count()
            return {
                "available": True,
                "collection_name": COLLECTION_NAME,
                "document_count": count,
                "db_dir": self.db_dir,
            }
        except Exception as e:
            return {"available": False, "error": str(e)}


# ==================== 便捷函数 ====================

# 全局单例（懒加载）
_engine: Optional[RAGEngine] = None


def get_engine() -> RAGEngine:
    """获取全局 RAG 引擎实例（懒加载，自动构建索引）"""
    global _engine
    if _engine is None:
        _engine = RAGEngine()
        # 如果索引不存在则自动构建
        if not os.path.exists(os.path.join(CHROMA_DB_DIR)):
            logger.info("首次使用，正在构建 RAG 索引...")
            result = _engine.build_index()
            if result["success"]:
                logger.info(f"RAG 索引构建成功: {result['chunks_created']} 个 chunk")
            else:
                logger.warning(f"RAG 索引构建失败: {result.get('error')}")
    return _engine


def build_index(**kwargs) -> Dict[str, Any]:
    """便捷函数：构建索引"""
    return get_engine().build_index(**kwargs)


def search(
    query_text: str,
    top_k: int = 5,
) -> List[Tuple[str, float, Dict[str, Any]]]:
    """便捷函数：检索知识库"""
    return get_engine().search(query_text, top_k=top_k)


def search_formatted(query_text: str, top_k: int = 5) -> str:
    """便捷函数：检索并格式化"""
    return get_engine().search_formatted(query_text, top_k=top_k)


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("🧪 RAG 引擎测试")
    print("=" * 60)

    engine = RAGEngine()

    # 构建索引
    print("\n📚 构建索引...")
    result = engine.build_index()
    print(f"  结果: {result}")

    # 检索测试
    test_queries = [
        "杭州有什么好玩的景点推荐？",
        "亲子家庭适合什么类型的酒店？",
        "如何写一篇小红书的旅游推广文案？",
        "文创产品设计有什么案例？",
    ]

    print("\n🔍 检索测试:")
    for query in test_queries:
        print(f"\n  查询: {query}")
        results = engine.search(query, top_k=3)
        if results:
            for i, (doc, score, meta) in enumerate(results, 1):
                print(f"    [{i}] 分数={score:.3f} 来源={meta.get('source', '?')}")
                print(f"        摘要: {doc[:100]}...")
        else:
            print(f"    (无结果)")

    # 格式化输出测试
    print("\n📝 格式化输出:")
    formatted = engine.search_formatted("旅游行程规划模板", top_k=2)
    print(formatted[:500])

    print("\n✅ 测试完成")
