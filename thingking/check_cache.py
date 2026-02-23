import os
import sys

# 将 src 加入路径以便导入
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

# 检查HuggingFace缓存目录
cache_dir = os.path.expanduser('~/.cache/huggingface/hub')
print(f"HuggingFace 缓存目录: {cache_dir}")
print(f"目录存在: {os.path.exists(cache_dir)}")

if os.path.exists(cache_dir):
    # 查找模型相关目录
    model_dirs = []
    for item in os.listdir(cache_dir):
        if 'sentence-transformers' in item.lower() or 'all-minilm' in item.lower():
            model_dirs.append(item)
    
    print(f"\n找到 {len(model_dirs)} 个相关模型目录:")
    for model_dir in model_dirs[:5]:
        model_path = os.path.join(cache_dir, model_dir)
        if os.path.isdir(model_path):
            # 计算目录大小
            total_size = 0
            file_count = 0
            for root, dirs, files in os.walk(model_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        total_size += os.path.getsize(file_path)
                        file_count += 1
                    except:
                        pass
            size_mb = total_size / (1024 * 1024)
            print(f"  - {model_dir}")
            print(f"    大小: {size_mb:.2f} MB ({file_count} 个文件)")

print("\n" + "="*60)
print("说明:")
print("1. 如果看到模型目录，说明模型已缓存到本地")
print("2. 40-50秒的加载时间是正常的，包括:")
print("   - 从磁盘读取模型文件")
print("   - 初始化PyTorch模型")
print("   - 加载权重到内存")
print("3. 如果模型没有缓存，首次下载可能需要几分钟")
print("="*60)
