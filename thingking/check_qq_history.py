import sys
import os
import time

# 将 src 加入路径以便导入
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

from rag_memory import RAGMemory

def main():
    print("=" * 60)
    print("QQ History Check Tool")
    print("=" * 60)

    try:
        # 连接到 RAG 服务
        client = RAGMemory(persist_directory="chroma_db_layered")
        
        # 尝试搜索最近的记录（通过空查询或特定关键词）
        # 注意：这里我们使用 search_qq_history 接口
        # 为了获取所有记录，可以尝试搜索通用词，例如 " " 或者 "202" (年份)
        
        print("\n[Action] Fetching recent QQ history (via search)...")
        results = client.search_qq_history(query=" ", k=20)
        
        if not results:
            print("[Result] No records found or connection failed.")
            print("         Please ensure 'rag_server.py' is running.")
            return

        print(f"[Result] Found {len(results)} records:\n")
        
        for i, res in enumerate(results, 1):
            # res 是字符串形式的文档内容
            print(f"--- Record {i} ---")
            print(res.strip())
            print()
            
    except Exception as e:
        print(f"[Error] Failed to fetch history: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
