import re
import requests
import os
import time
from pathlib import Path
import sys

# 确保可以从项目根目录导入 config（兼容从不同工作目录运行 speaking 模块）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import TTS_HTTP_BASE_URL

# ================= 配置区域 =================
# API 地址
BASE_URL = TTS_HTTP_BASE_URL

base_path = Path(".").resolve()
list_path = base_path / "history_voice" / "voice_list.txt"
illegal_pattern = r'[\\/:*?"<>|]'

# 参考音频配置 - 根据语种动态设置
def get_ref_audio_config(target_lang):
    """根据目标语种返回对应的参考音频配置"""
    # 使用模块级别的 base_path，避免重复定义
    if target_lang == 'zh':
        return {
            'ref_audio_path': str(base_path / "temp_by bines" / "3星结束行动zh.wav"),
            'prompt_text': "从您的指挥里我学到了很多，说不定以后我也能用得上哦。",
            'prompt_lang': 'zh',
            # 'seed': 1935408000
            'seed': -1
        }
    elif target_lang == 'ja':
        return {
            'ref_audio_path': str(base_path / "temp_by bines" / "3星结束行动ja.wav"),
            'prompt_text': "ドクターの指揮はすっごくお勉強になるね。あたしもいつか使う時が来るかも。",
            'prompt_lang': 'ja',
            # 'seed': 1537048296
            'seed': -1
        }
    else:
        # 默认使用zh配置
        return {
            'ref_audio_path': str(base_path / "temp_by bines" / "3星结束行动zh.wav"),
            'prompt_text': "从您的指挥里我学到了很多，说不定以后我也能用得上哦。",
            'prompt_lang': 'zh',
            # 'seed': 1935408000
            'seed': -1
        }


# ================= 功能函数 =================

def append_temple(origin_path, temple_txt, temple_lang, append):
    """将生成的语音作为模板放入对应的已生成语音文件夹"""
    try:
        dir_path = base_path / "history_voice" / temple_lang
        origin_file = Path(origin_path).resolve()  # 解析绝对路径，提高稳定性
        
        # 检查源文件是否存在
        if not origin_file.exists():
            print(f"⚠️ 警告: 源文件不存在: {origin_file}")
            return False
        
        # 生成安全的文件名：只使用文件名部分，避免路径分隔符问题
        safe_text = re.sub(illegal_pattern, '_', temple_txt).strip()
        timestamp = int(time.time() * 1000)  # 使用毫秒时间戳提高唯一性
        origin_name = origin_file.name  # 只使用文件名，不包含路径
        target_file = dir_path / f"{safe_text}_{timestamp}_{origin_name}"

        # 创建目录（如果不存在）
        if not dir_path.is_dir():
            dir_path.mkdir(parents=True, exist_ok=True)

        # 移动/重命名文件
        origin_file.rename(target_file)

        # 写入配置文件
        if append:
            # 确保 list_path 的父目录存在
            list_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file=list_path, mode='a', encoding='utf-8') as file:
                # 关键修复：将Path对象转为字符串后写入
                file.write(f"{target_file.resolve()}\n")

        return True
    except FileNotFoundError:
        print(f"❌ 错误: 源文件不存在: {origin_path}")
        return False
    except PermissionError:
        print(f"❌ 错误: 没有权限访问文件: {origin_path}")
        return False
    except Exception as e:
        print(f"❌ append_temple 失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def tts_generate_streaming(target_text, target_lang, chunk_callback=None):
    """
    流式生成语音（真正的流式，边接收边处理）
    
    Args:
        target_text: 目标文本
        target_lang: 目标语言
        chunk_callback: 回调函数，每收到一个数据块时调用 callback(chunk: bytes)
    
    Returns:
        bytes: 完整的音频数据（如果不需要完整数据，可以忽略返回值）
    """
    url = f"{BASE_URL}/tts"
    ref_config = get_ref_audio_config(target_lang)
    
    payload = {
        "text": target_text,
        "text_lang": target_lang,
        "ref_audio_path": ref_config['ref_audio_path'],
        "prompt_text": ref_config['prompt_text'],
        "prompt_lang": ref_config['prompt_lang'],
        "aux_ref_audio_paths": [],
        "text_split_method": "cut0",
        "speed_factor": 1.0,
        "fragment_interval": 0.4,
        "batch_size": 1,
        "parallel_infer": True,
        "top_k": 5,
        "top_p": 1.0,
        "temperature": 0.8,
        "repetition_penalty": 1.35,
        "media_type": "wav",
        "streaming_mode": True,
        "seed": ref_config['seed']
    }
    
    preview_text = target_text[:20] if target_text and len(target_text) > 20 else (target_text or "[空文本]")
    print(f"🎤 开始流式合成: {preview_text}...")
    start_time = time.time()
    
    try:
        resp = requests.post(url, json=payload, timeout=300, stream=True)
        resp.raise_for_status()
        
        audio_chunks = []
        chunk_count = 0
        first_chunk_time = None
        
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                if first_chunk_time is None:
                    first_chunk_time = time.time()
                    first_chunk_delay = first_chunk_time - start_time
                    print(f"📥 收到第一个数据块，延迟: {first_chunk_delay:.2f}s")
                
                audio_chunks.append(chunk)
                chunk_count += 1
                
                # 立即调用回调，实现真正的流式处理
                if chunk_callback:
                    try:
                        chunk_callback(chunk)
                    except Exception as e:
                        print(f"⚠️ 回调函数执行失败: {e}")
                
                if chunk_count % 10 == 0:
                    current_size = sum(len(c) for c in audio_chunks)
                    print(f"📥 已接收: {current_size} bytes ({chunk_count} 块)", end='\r')
        
        audio_data = b''.join(audio_chunks)
        
        if first_chunk_time:
            total_time = time.time() - start_time
            print(f"\n✅ 流式接收完成！总耗时: {total_time:.2f}s | 音频大小: {len(audio_data)} bytes")
        else:
            print("⚠️ 警告: 流式接收未收到任何数据")
            return None
        
        return audio_data
        
    except Exception as e:
        print(f"❌ 流式合成失败: {e}")
        return None


def tts_generate(target_text, target_lang, append=True, use_streaming=True):
    """
    执行语音合成（兼容旧接口）
    
    Args:
        target_text: 目标文本
        target_lang: 目标语言
        append: 是否追加到模板库
        use_streaming: 是否使用流式传输（True时使用stream=True逐步接收，False时等待完整响应）
    
    Returns:
        bytes: 完整的音频数据，如果失败返回None
    """
    url = f"{BASE_URL}/tts"

    # 根据语种获取参考音频配置
    ref_config = get_ref_audio_config(target_lang)

    # 构建全参数 Payload
    payload = {
        # --- 核心内容 ---
        "text": target_text,  # 目标文本
        "text_lang": target_lang,  # 目标语言
        "ref_audio_path": ref_config['ref_audio_path'],  # 主参考音频
        "prompt_text": ref_config['prompt_text'],  # 参考文本
        "prompt_lang": ref_config['prompt_lang'],  # 参考语言
        "aux_ref_audio_paths": [],  # 辅参考音频列表（已移除）

        # --- 切分与速度 ---
        "text_split_method": "cut0",
        "speed_factor": 1.0,  # 语速
        "fragment_interval": 0.4,  # 分段间隔(秒)

        # --- 高级参数 (对应WebUI截图) ---
        "batch_size": 1,  # 【注意】显卡好可设为 4-8，显卡一般建议 1
        "parallel_infer": True,  # 开启并行推理 (配合 batch_size > 1 使用)
        "top_k": 5,  # 采样率
        "top_p": 1.0,  # 采样率
        "temperature": 0.8,  # 温度 (0.8-1.0)
        "repetition_penalty": 1.35,  # 重复惩罚 (防止复读机)

        # --- 其他 ---
        "media_type": "wav",  # 返回音频格式
        "streaming_mode": use_streaming,  # 是否流式返回（与use_streaming参数保持一致）

        "seed": ref_config['seed']  # 根据语种使用对应的随机种子
    }

    # 安全地截取文本用于显示（避免空文本或过短文本的问题）
    preview_text = target_text[:20] if target_text and len(target_text) > 20 else (target_text or "[空文本]")
    print(f"🎤 开始合成: {preview_text}...")
    start_time = time.time()

    try:
        # 发送 POST 请求
        # 关键修复：如果 use_streaming=True，使用 stream=True 参数实现真正的流式接收
        resp = requests.post(
            url, 
            json=payload, 
            timeout=300,  # 添加超时设置（5分钟）
            stream=use_streaming  # 启用流式传输
        )
        resp.raise_for_status()

        # 根据是否使用流式传输，采用不同的数据接收方式
        if use_streaming:
            # 流式接收：逐步接收数据块，边下边处理
            audio_chunks = []
            chunk_count = 0
            first_chunk_time = None
            
            # 使用 iter_content 逐步接收数据块（chunk_size=8192 bytes）
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:  # 过滤掉 keep-alive 空块
                    if first_chunk_time is None:
                        first_chunk_time = time.time()
                        first_chunk_delay = first_chunk_time - start_time
                        print(f"📥 收到第一个数据块，延迟: {first_chunk_delay:.2f}s")
                    
                    audio_chunks.append(chunk)
                    chunk_count += 1
                    
                    # 每收到10个块打印一次进度（避免输出过多）
                    if chunk_count % 10 == 0:
                        current_size = sum(len(c) for c in audio_chunks)
                        print(f"📥 已接收: {current_size} bytes ({chunk_count} 块)", end='\r')
            
            # 合并所有数据块
            audio_data = b''.join(audio_chunks)
            
            if first_chunk_time:
                total_time = time.time() - start_time
                print(f"\n✅ 流式接收完成！总耗时: {total_time:.2f}s | 音频大小: {len(audio_data)} bytes | 块数: {chunk_count}")
            else:
                print("⚠️ 警告: 流式接收未收到任何数据")
                return None
        else:
            # 非流式：等待所有数据下载完毕（传统方式）
            audio_data = resp.content
            
            if not audio_data:
                print("⚠️ 警告: 服务器返回空内容")
                return None
            
            cost_time = time.time() - start_time
            print(f"✅ 合成完毕！耗时: {cost_time:.2f}s | 音频大小: {len(audio_data)} bytes")

        # 保存文件（已注释，如需要可取消注释）
        # output_file = "output_result.wav"
        # with open(output_file, "wb") as f:
        #    f.write(audio_data)
        # append_temple(output_file, target_text, target_lang, append)

        return audio_data

    except requests.exceptions.Timeout:
        print(f"❌ 请求超时: TTS 服务响应时间超过 5 分钟")
        return None
    except requests.exceptions.ConnectionError:
        print(f"❌ 连接失败: 无法连接到 TTS 服务 ({BASE_URL})")
        print(f"   请确保 TTS 服务正在运行")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"❌ HTTP 错误: {e}")
        try:
            print(f"   服务器返回: {e.response.text[:200]}")  # 只显示前200字符
        except:
            pass
        return None
    except requests.exceptions.RequestException as e:
        print(f"❌ 请求失败: {e}")
        return None  # 返回 None 表示失败，调用者需要处理