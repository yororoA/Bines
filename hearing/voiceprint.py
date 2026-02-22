"""
声纹识别模块
优先使用 resemblyzer，如果不可用则使用 librosa 提取 MFCC 特征
"""
import os
import numpy as np
import pickle
from pathlib import Path

# 尝试导入 resemblyzer（优先使用）
RESEMBLYZER_AVAILABLE = False
try:
    from resemblyzer import VoiceEncoder, preprocess_wav
    RESEMBLYZER_AVAILABLE = True
except ImportError:
    pass

# 尝试导入 librosa（备用方案）
LIBROSA_AVAILABLE = False
try:
    import librosa
    LIBROSA_AVAILABLE = True
except ImportError:
    pass

if not RESEMBLYZER_AVAILABLE and not LIBROSA_AVAILABLE:
    print("声纹识别功能不可用：resemblyzer 和 librosa 都未安装")
    print("   推荐安装: pip install resemblyzer")
    print("   或备用方案: pip install librosa")

class VoiceprintManager:
    """声纹管理器"""
    
    def __init__(self, voiceprint_dir="voiceprints"):
        """
        初始化声纹管理器
        
        Args:
            voiceprint_dir: 声纹文件存储目录
        """
        self.voiceprint_dir = Path(voiceprint_dir)
        self.voiceprint_dir.mkdir(exist_ok=True)
        
        # 检查可用的库
        self.use_resemblyzer = RESEMBLYZER_AVAILABLE
        self.use_librosa = LIBROSA_AVAILABLE  # 始终允许使用 librosa 作为备用
        
        if self.use_resemblyzer:
            try:
                self.encoder = VoiceEncoder()
                print("声纹编码器加载成功 (使用 resemblyzer)")
            except Exception as e:
                print(f"resemblyzer 编码器加载失败: {e}")
                self.encoder = None
                # 回退到 librosa
                if LIBROSA_AVAILABLE:
                    self.use_resemblyzer = False
                    self.use_librosa = True
                    print("   回退到 librosa MFCC 特征提取")
        elif self.use_librosa:
            self.encoder = None
            print("使用 librosa MFCC 特征提取")
        else:
            self.encoder = None
            print("声纹识别功能不可用")
        
        # 加载已注册的声纹
        self.registered_voiceprints = {}
        self.load_voiceprints()
    
    def load_voiceprints(self):
        """加载所有已注册的声纹"""
        if not self.use_resemblyzer and not self.use_librosa:
            return
        
        for file_path in self.voiceprint_dir.glob("*.pkl"):
            try:
                with open(file_path, 'rb') as f:
                    data = pickle.load(f)
                    user_id = file_path.stem
                    self.registered_voiceprints[user_id] = data
                    print(f"加载声纹: {user_id}")
            except Exception as e:
                print(f"加载声纹失败 {file_path}: {e}")
    
    def extract_voiceprint(self, audio_data, sample_rate=16000):
        """
        从音频数据中提取声纹特征
        
        Args:
            audio_data: 音频数据 (numpy array, float32, 范围 [-1, 1])
            sample_rate: 采样率，默认16000
        
        Returns:
            numpy array: 声纹特征向量，如果失败返回None
        """
        embedding = None
        
        if self.use_resemblyzer and self.encoder:
            # 使用 resemblyzer
            try:
                # 确保音频是 float32 格式，范围在 [-1, 1]
                if audio_data.dtype != np.float32:
                    if audio_data.dtype == np.int16:
                        audio_float = audio_data.astype(np.float32) / 32768.0
                    else:
                        audio_float = audio_data.astype(np.float32)
                else:
                    audio_float = audio_data
                
                # 确保范围在有效区间
                audio_float = np.clip(audio_float, -1.0, 1.0)
                
                # 尝试不同的方式调用 resemblyzer
                # 方式1: 跳过 preprocess_wav，直接使用 embed_utterance（某些版本支持直接输入 float32）
                try:
                    embedding = self.encoder.embed_utterance(audio_float)
                    return embedding
                except Exception as e1:
                    # 方式2: 使用 preprocess_wav 预处理（float32）
                    try:
                        wav = preprocess_wav(audio_float, sample_rate)
                        embedding = self.encoder.embed_utterance(wav)
                        return embedding
                    except Exception as e2:
                        # 方式3: 转换为 int16 再调用 preprocess_wav
                        try:
                            audio_int16 = (audio_float * 32767).astype(np.int16)
                            wav = preprocess_wav(audio_int16, sample_rate)
                            embedding = self.encoder.embed_utterance(wav)
                            return embedding
                        except Exception as e3:
                            # 如果所有方式都失败，抛出异常
                            raise Exception(f"All methods failed: direct embed ({e1}), float32 preprocess ({e2}), int16 preprocess ({e3})")
                return embedding
            except Exception as e:
                print(f"resemblyzer 声纹提取失败: {e}")
                # 如果 resemblyzer 失败，尝试回退到 librosa
                if LIBROSA_AVAILABLE:
                    print("   尝试回退到 librosa MFCC 特征...")
                    # 继续执行下面的 librosa 分支
                else:
                    return None
        
        # 如果 resemblyzer 失败或不可用，使用 librosa
        if self.use_librosa and embedding is None:
            # 使用 librosa MFCC 特征
            try:
                import librosa
                
                # 确保音频是 float32 格式，范围在 [-1, 1]
                if audio_data.dtype != np.float32:
                    if audio_data.dtype == np.int16:
                        audio_data = audio_data.astype(np.float32) / 32768.0
                    else:
                        audio_data = audio_data.astype(np.float32)
                
                # 确保音频长度足够（至少0.5秒）
                min_length = int(sample_rate * 0.5)
                if len(audio_data) < min_length:
                    return None
                
                # 提取更丰富的声纹特征
                # 1. MFCC 特征
                mfccs = librosa.feature.mfcc(
                    y=audio_data,
                    sr=sample_rate,
                    n_mfcc=13,
                    n_fft=2048,
                    hop_length=512
                )
                
                # 2. 色度特征（音色相关）
                chroma = librosa.feature.chroma(y=audio_data, sr=sample_rate)
                
                # 3. 谱质心（音色亮度）
                spectral_centroids = librosa.feature.spectral_centroid(y=audio_data, sr=sample_rate)[0]
                
                # 计算统计量
                mfcc_mean = np.mean(mfccs, axis=1)
                mfcc_std = np.std(mfccs, axis=1)
                chroma_mean = np.mean(chroma, axis=1)
                chroma_std = np.std(chroma, axis=1)
                centroid_mean = np.mean(spectral_centroids)
                centroid_std = np.std(spectral_centroids)
                
                # 组合成更丰富的特征向量（13+13+12+12+1+1=52维）
                embedding = np.concatenate([
                    mfcc_mean, mfcc_std,
                    chroma_mean, chroma_std,
                    [centroid_mean], [centroid_std]
                ])
                
                # 归一化特征向量
                norm = np.linalg.norm(embedding)
                if norm > 0:
                    embedding = embedding / norm
                
                return embedding
            except Exception as e:
                print(f"librosa 声纹提取失败: {e}")
                return None
        
        return None
    
    def register_voiceprint(self, user_id, audio_data, sample_rate=16000):
        """
        注册用户声纹
        
        Args:
            user_id: 用户ID
            audio_data: 音频数据
            sample_rate: 采样率
        
        Returns:
            bool: 是否注册成功
        """
        if not self.use_resemblyzer and not self.use_librosa:
            print("声纹识别库未安装，无法注册")
            return False
        
        embedding = self.extract_voiceprint(audio_data, sample_rate)
        if embedding is None:
            return False
        
        # 保存声纹
        file_path = self.voiceprint_dir / f"{user_id}.pkl"
        try:
            with open(file_path, 'wb') as f:
                pickle.dump(embedding, f)
            self.registered_voiceprints[user_id] = embedding
            print(f"声纹注册成功: {user_id}")
            return True
        except Exception as e:
            print(f"声纹保存失败: {e}")
            return False
    
    def verify_voiceprint(self, audio_data, sample_rate=16000, threshold=None):
        """
        验证声纹是否匹配
        
        Args:
            audio_data: 待验证的音频数据
            sample_rate: 采样率
            threshold: 相似度阈值
                       - resemblyzer: 默认0.70（范围0-1，越高越严格）
                       - librosa MFCC: 默认0.65（MFCC特征相似度通常较低，需要调整阈值）
        
        Returns:
            tuple: (是否匹配, 最高相似度, 匹配的用户ID)
        """
        # 根据使用的库设置默认阈值
        # resemblyzer: 0.70（降低阈值，提高识别率）
        # MFCC特征的相似度通常较低（0.4-0.8），需要降低阈值
        if threshold is None:
            threshold = 0.50 if self.use_resemblyzer else 0.6
        
        if (not self.use_resemblyzer and not self.use_librosa) or not self.registered_voiceprints:
            # 如果没有注册的声纹，默认通过（向后兼容）
            return (True, 1.0, None)
        
        embedding = self.extract_voiceprint(audio_data, sample_rate)
        if embedding is None:
            return (False, 0.0, None)
        
        # 与所有已注册的声纹比对
        best_match = None
        best_similarity = 0.0
        
        for user_id, registered_embedding in self.registered_voiceprints.items():
            # 计算余弦相似度
            similarity = np.dot(embedding, registered_embedding) / (
                np.linalg.norm(embedding) * np.linalg.norm(registered_embedding)
            )
            
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = user_id
        
        # 判断是否匹配
        is_match = best_similarity >= threshold
        
        if is_match:
            print(f"声纹匹配成功: {best_match} (相似度: {best_similarity:.3f})")
        else:
            print(f"声纹不匹配 (最高相似度: {best_similarity:.3f}, 阈值: {threshold})")
        
        return (is_match, best_similarity, best_match)
    
    def has_registered_voiceprints(self):
        """检查是否有已注册的声纹"""
        return len(self.registered_voiceprints) > 0
