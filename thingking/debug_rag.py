import sys
import os
import json
import datetime
from pathlib import Path

# 将 src 加入路径以便导入
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

from rag_memory import RAGMemory


def main():
    print("=" * 60)
    print("RAG 记忆诊断工具（经由 rag_server）")
    print("=" * 60)

    # 创建客户端实例（不会在本进程加载任何重型依赖）
    rag = RAGMemory(persist_directory="chroma_db_layered")

    # 获取集合统计信息（通过服务端）
    try:
        count = rag.collection_count()
        print(f"\n[Status] 当前记忆库中共有 {count} 条记录。")
    except Exception as e:
        print(f"[Warning] 无法获取统计信息: {e}")

    print("\n" + "=" * 60)
    print(" RAG 记忆诊断工具")
    print("=" * 60)
    print(" 命令:")
    print("  - [文本]: 检索相关记忆")
    print("  - stats: 显示详细统计信息")
    print("  - export: 导出全部记忆到JSON文件")
    print("  - export:路径/文件名.json : 导出到指定路径")
    print("  - del:关键词 : 删除包含关键词的记忆")
    print("  - date:2026/01/15 : 删除指定日期的记忆")
    print("  - date:2026/01/01-2026/01/31 : 删除日期范围内的记忆")
    print("  - q: 退出")
    print("="*60)
    
    while True:
        try:
            query = input("\n> ").strip()
        except UnicodeDecodeError:
            continue
            
        if not query: continue
        if query.lower() == 'q': break
        
        # --- 统计信息 ---
        if query.lower() == 'stats':
            try:
                all_data = rag.collection_get()
                ids = all_data.get('ids', [])
                docs = all_data.get('documents', [])
                metadatas = all_data.get('metadatas', [])
                
                total_count = len(ids)
                print(f"\n{'='*60}")
                print(f"📊 记忆库统计信息")
                print(f"{'='*60}")
                print(f"总记忆数: {total_count}")
                
                if total_count > 0:
                    # 按类型统计
                    type_count = {}
                    access_count_sum = 0
                    timestamp_list = []
                    
                    for i, meta in enumerate(metadatas):
                        mem_type = meta.get('type', 'unknown')
                        type_count[mem_type] = type_count.get(mem_type, 0) + 1
                        access_count_sum += meta.get('access_count', 1)
                        if 'timestamp' in meta:
                            timestamp_list.append(meta['timestamp'])
                    
                    print(f"\n按类型分布:")
                    for mem_type, count in sorted(type_count.items()):
                        percentage = (count / total_count) * 100
                        print(f"  - {mem_type}: {count} ({percentage:.1f}%)")
                    
                    print(f"\n访问统计:")
                    avg_access = access_count_sum / total_count if total_count > 0 else 0
                    print(f"  - 总访问次数: {access_count_sum}")
                    print(f"  - 平均访问次数: {avg_access:.2f}")
                    
                    if timestamp_list:
                        timestamp_list.sort()
                        oldest = datetime.datetime.fromtimestamp(timestamp_list[0])
                        newest = datetime.datetime.fromtimestamp(timestamp_list[-1])
                        print(f"\n时间范围:")
                        print(f"  - 最早记忆: {oldest.strftime('%Y-%m-%d %H:%M:%S')}")
                        print(f"  - 最新记忆: {newest.strftime('%Y-%m-%d %H:%M:%S')}")
                        span_days = (timestamp_list[-1] - timestamp_list[0]) / 86400
                        print(f"  - 时间跨度: {span_days:.1f} 天")
                else:
                    print("记忆库为空")
                
                print(f"{'='*60}\n")
            except Exception as e:
                print(f"获取统计信息失败: {e}")
                import traceback
                traceback.print_exc()
            continue
        
        # --- 导出功能 ---
        if query.lower().startswith('export'):
            try:
                export_path = query[6:].strip() if len(query) > 6 else None
                
                print("正在导出全部记忆...")
                all_data = rag.collection_get()
                ids = all_data.get('ids', [])
                docs = all_data.get('documents', [])
                metadatas = all_data.get('metadatas', [])
                
                if not ids:
                    print("记忆库为空，无需导出。")
                    continue
                
                # 构建导出数据
                export_data = {
                    'export_time': datetime.datetime.now().isoformat(),
                    'total_count': len(ids),
                    'memories': []
                }
                
                for i in range(len(ids)):
                    memory_item = {
                        'id': ids[i],
                        'content': docs[i] if i < len(docs) else '',
                        'metadata': metadatas[i] if i < len(metadatas) else {}
                    }
                    
                    # 添加格式化时间戳
                    if 'timestamp' in memory_item['metadata']:
                        ts = memory_item['metadata']['timestamp']
                        memory_item['formatted_time'] = datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
                    
                    export_data['memories'].append(memory_item)
                
                # 确定导出路径
                if export_path:
                    output_path = Path(export_path)
                else:
                    # 默认导出到当前目录，使用时间戳命名
                    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
                    output_path = Path(__file__).parent / f"rag_memory_export_{timestamp}.json"
                
                # 确保目录存在
                output_path.parent.mkdir(parents=True, exist_ok=True)
                
                # 写入文件
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(export_data, f, ensure_ascii=False, indent=2)
                
                file_size = output_path.stat().st_size / 1024  # KB
                print(f"✓ 导出成功！")
                print(f"  文件路径: {output_path.absolute()}")
                print(f"  导出数量: {len(ids)} 条记忆")
                print(f"  文件大小: {file_size:.2f} KB")
                
            except Exception as e:
                print(f"导出失败: {e}")
                import traceback
                traceback.print_exc()
            continue
        
        # --- 按日期删除逻辑 ---
        if query.startswith("date:"):
            date_param = query[5:].strip()
            print(f"正在扫描日期/范围: {date_param} ...")
            
            try:
                # 获取全量数据进行 Python 端过滤
                all_data = rag.collection_get()
                ids = all_data['ids']
                docs = all_data['documents']
                
                if not ids:
                    print("数据库为空。")
                    continue

                ids_to_delete = []
                docs_to_delete = []
                
                is_range = "-" in date_param
                start_dt = None
                end_dt = None
                import datetime as _dt_mod, re as _re_mod
                
                if is_range:
                    try:
                        s_str, e_str = date_param.split("-")
                        start_dt = _dt_mod.datetime.strptime(s_str.strip(), "%Y/%m/%d")
                        end_dt = _dt_mod.datetime.strptime(e_str.strip(), "%Y/%m/%d")
                    except ValueError:
                         print("日期格式错误。请使用 YYYY/MM/DD 或 YYYY/MM/DD-YYYY/MM/DD")
                         continue
                
                for i, doc_content in enumerate(docs):
                    # 提取日期: "User: [2026/01/15]:"
                    match = _re_mod.search(r"User: \[(\d{4}/\d{2}/\d{2})(?:\s+\d{2}:\d{2})?\]:", doc_content)
                    if match:
                        doc_date_str = match.group(1)
                        should_delete = False
                        
                        if is_range:
                            try:
                                doc_dt = _dt_mod.datetime.strptime(doc_date_str, "%Y/%m/%d")
                                if start_dt <= doc_dt <= end_dt:
                                    should_delete = True
                            except: pass
                        else:
                            if doc_date_str == date_param:
                                should_delete = True
                        
                        if should_delete:
                            ids_to_delete.append(ids[i])
                            docs_to_delete.append(doc_content)
                
                if not ids_to_delete:
                    print("未找到该时间段的记忆。")
                else:
                    print(f"找到 {len(ids_to_delete)} 条记录:")
                    # 列出所有找到的记录
                    for k, content in enumerate(docs_to_delete):
                        preview = content.replace('\n', ' ')[:100]
                        print(f"[{k+1}] {preview}...")
                    
                    choice = input(f"\n请输入要删除的序号 (例如 1,3 或 1-5), 输入 'all' 删除全部, 'n' 取消: ").strip().lower()
                    
                    final_ids_to_del = []
                    
                    if choice == 'n' or not choice:
                        print("已取消。")
                    elif choice == 'all':
                        final_ids_to_del = ids_to_delete
                    else:
                        try:
                            # 支持 1,2,3 和 1-3
                            parts = choice.replace('，', ',').split(',')
                            indexes = set()
                            for part in parts:
                                part = part.strip()
                                if '-' in part:
                                    s, e = map(int, part.split('-'))
                                    indexes.update(range(s, e+1))
                                else:
                                    indexes.add(int(part))
                            
                            for idx in indexes:
                                if 1 <= idx <= len(ids_to_delete):
                                    final_ids_to_del.append(ids_to_delete[idx-1])
                        except ValueError:
                            print("输入格式错误。")
                    
                    if final_ids_to_del:
                        rag.collection_delete(ids=final_ids_to_del)
                        print(f"成功删除 {len(final_ids_to_del)} 条记忆。")
            except Exception as e:
                print(f"操作失败: {e}")
            continue

        # --- 删除逻辑 ---
        if query.startswith("del:"):
            keyword = query[4:].strip()
            if not keyword:
                print("请指定删除关键词，例如 del:废物")
                continue
            
            # 使用包含搜索找到相关文档的 ID
            print(f"正在搜索并删除包含 '{keyword}' 的记忆...")
            # Chroma 的 get 方法支持 where_document={"$contains": ...} 但比较慢且版本差异大
            # 简单方法：先检索出一批，只要 content 包含 keyword 就删
            
            # 1. 使用 where_document contains（经由 RAG 服务）
            try:
                results = rag.collection_get(where_document={"$contains": keyword})
                ids_to_delete = results.get("ids", [])
                documents_to_delete = results.get("documents", [])

                if not ids_to_delete:
                    print(f"没有找到包含 '{keyword}' 的记忆。")
                else:
                    print(f"找到 {len(ids_to_delete)} 条相关记录:")
                    for i, doc in enumerate(documents_to_delete):
                        print(f"[{i+1}] {doc[:60]}...")

                    confirm = input(f"确认删除这 {len(ids_to_delete)} 条吗? (y/n): ")
                    if confirm.lower() == 'y':
                        rag.collection_delete(ids=ids_to_delete)
                        print("删除成功！")
                    else:
                        print("取消删除。")
            except Exception as e:
                print(f"删除操作失败: {e}")

            continue
        # ----------------
        
        print(f"\n正在检索与 '{query}' 相关的记忆...")
        try:
            import math
            import re
            
            # 1. 手动调用底层 search 查看原始分（通过 RAG 服务）
            raw_results = rag.similarity_search_with_score(query, k=10)
            
            if not raw_results:
                print("未找到相关记忆。")
                continue
            
            print(f"\n{'='*60}")
            print(f"📋 原始检索结果 (Top {len(raw_results)})")
            print(f"{'='*60}")
            print(f"说明: Distance = L2距离(越小越相似), Score = 加权分数(越大越好)")
            print(f"{'-'*60}")
            
            for idx, item in enumerate(raw_results, 1):
                distance = item.get("score", 0.0)
                meta = item.get("metadata") or {}
                content = item.get("content", "")
                count = meta.get("access_count", 1)
                relevance = 1.0 / (1.0 + distance)
                final_score = relevance * (1 + math.log(count))
                
                parts = content.split('\nAssistant: ')
                user_part = parts[0].replace('User: ', '') if parts else ''
                assistant_part = parts[1] if len(parts) > 1 else ''
                
                # 提取日期
                date_match = re.search(r'\[(\d{4}/\d{2}/\d{2})(?:\s+\d{2}:\d{2})?\]', user_part)
                date_str = date_match.group(1) if date_match else '未知'
                
                print(f"\n[{idx}] Distance: {distance:.4f} | Score: {final_score:.4f} | Count: {count}")
                print(f"    日期: {date_str} | 类型: {meta.get('type', 'conversation')}")
                print(f"    👤 User: {user_part[:100]}{'...' if len(user_part) > 100 else ''}")
                if assistant_part:
                    print(f"    🤖 Assistant: {assistant_part[:100]}{'...' if len(assistant_part) > 100 else ''}")
            
            # 2. 调用应用层接口查看最终结果 (去重和截断后)
            print(f"\n{'='*60}")
            print(f"✅ 实际注入 Prompt 的内容 (Top 4)")
            print(f"{'='*60}")
            final_ctx = rag.get_relevant_context(query, k=4)
            if not final_ctx:
                print("(无相关结果 - 可能相似度过低被过滤或数据库为空)")
            else:
                for i, content in enumerate(final_ctx, 1):
                    parts = content.split('\nAssistant: ')
                    user_part = parts[0].replace('User: ', '') if parts else ''
                    assistant_part = parts[1] if len(parts) > 1 else ''
                    
                    print(f"\n[{i}]")
                    print(f"    👤 User: {user_part[:150]}{'...' if len(user_part) > 150 else ''}")
                    if assistant_part:
                        print(f"    🤖 Assistant: {assistant_part[:150]}{'...' if len(assistant_part) > 150 else ''}")
            
            print(f"\n{'='*60}\n")
                
        except Exception as e:
            print(f"检索出错: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    main()
