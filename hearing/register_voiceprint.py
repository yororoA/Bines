"""
声纹注册工具
用于注册用户的声纹特征
"""
import os
import sys
import numpy as np
import pyaudio
import time
# 确保你已经正确安装并导入了 VoiceprintManager
try:
    from voiceprint import VoiceprintManager
except ImportError:
    print("找不到 voiceprint 模块，请确保 voiceprint.py 在当前目录下")
    sys.exit(1)

# 音频配置
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SAMPLES = 1600
FORMAT = pyaudio.paInt16
RECORD_SECONDS = 3

def record_audio(duration=3):
    """录制音频并转换为 float32 格式"""
    p = pyaudio.PyAudio()
    
    try:
        stream = p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SAMPLES
        )
        
        print(f"开始录制 {duration} 秒...")
        print("   请清晰地说一段话（建议说：你好，我是XXX）")
        
        frames = []
        # 循环次数计算
        for _ in range(0, int(SAMPLE_RATE / CHUNK_SAMPLES * duration)):
            data = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
            frames.append(data)
        
        stream.stop_stream()
        stream.close()
        
        # 1. 将字节流转换为 int16 numpy 数组
        audio_data = np.frombuffer(b''.join(frames), dtype=np.int16)
        
        # 2. 转换为 float32 并归一化 (-1.0 到 1.0)
        # 关键修改：先除法，再强制转回 float32，避免变成 float64
        audio_data = (audio_data / 32768.0).astype(np.float32)
        
        # 3. 移除静音或无效数据检测 (可选)
        if len(audio_data) == 0:
            print("录制数据为空")
            return None
            
        print(f"录制完成 (Shape: {audio_data.shape}, Dtype: {audio_data.dtype})")
        return audio_data
        
    except Exception as e:
        print(f"录制失败: {e}")
        return None
    finally:
        p.terminate()

def main():
    print("=" * 60)
    print("声纹注册工具 (已修复数据类型问题)")
    print("=" * 60)
    
    # 初始化声纹管理器
    try:
        voiceprint_manager = VoiceprintManager(voiceprint_dir="voiceprints")
    except Exception as e:
        print(f"初始化失败: {e}")
        return
    
    if not hasattr(voiceprint_manager, 'use_resemblyzer') or \
       (not voiceprint_manager.use_resemblyzer and not voiceprint_manager.use_librosa):
        print("\n声纹识别功能不可用，请检查 resemblyzer 库是否安装")
        return
    
    # 获取用户ID
    user_id = input("\n请输入用户ID（例如：user1）: ").strip()
    if not user_id:
        print("用户ID不能为空")
        return
    
    # 检查是否已注册
    if user_id in voiceprint_manager.registered_voiceprints:
        overwrite = input(f"用户 {user_id} 已存在，是否覆盖？(y/n): ").strip().lower()
        if overwrite != 'y':
            print("已取消")
            return
    
    # 录制音频
    print("\n准备录制...")
    time.sleep(1)
    
    # 获取录制的 float32 数据
    audio_data = record_audio(RECORD_SECONDS)
    
    if audio_data is None:
        print("录制失败，无法注册")
        return
    
    # 调试信息：再次确认发送给管理器的数据类型
    if not np.issubdtype(audio_data.dtype, np.floating):
        print(f"严重错误: 数据类型仍为 {audio_data.dtype}，必须是 float32/float64")
        return

    # 注册声纹
    print(f"\n正在注册声纹: {user_id}...")
    try:
        success = voiceprint_manager.register_voiceprint(user_id, audio_data, SAMPLE_RATE)
        
        if success:
            print(f"\n声纹注册成功！")
            print(f"   用户ID: {user_id}")
            print(f"   声纹文件: voiceprints/{user_id}.pkl")
        else:
            print(f"\n声纹注册失败 (Manager返回False)")
            
    except ValueError as ve:
        if "Audio data must be floating-point" in str(ve):
            print("\n错误: VoiceprintManager 内部可能再次转换了数据类型。")
            print("请检查 voiceprint.py 中是否将数据保存为 wav (int16) 后又读取失败。")
        else:
            print(f"\n数据错误: {ve}")
    except Exception as e:
        print(f"\n注册过程中发生未预料的错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已取消")