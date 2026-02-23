# -*- coding: utf-8 -*-
"""
情感分析模块
用于识别用户的情感状态
"""

import os
import sys

# 【修复】延迟导入 torch 和 transformers，避免未安装时导致整个模块崩溃
try:
    import torch
    from transformers import BertTokenizer, BertForSequenceClassification
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False
    torch = None
    print("[EmotionAnalysis] torch/transformers 未安装，情感分析功能将不可用")

# 添加项目根目录到Python路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 情感类别定义
EMOTION_CLASSES = {
    0: "愤怒",
    1: "厌恶",
    2: "恐惧",
    3: "快乐",
    4: "中立",
    5: "悲伤",
    6: "惊讶"
}

# 情感强度定义
EMOTION_INTENSITY = {
    "愤怒": 0.9,
    "厌恶": 0.7,
    "恐惧": 0.8,
    "快乐": 0.8,
    "中立": 0.5,
    "悲伤": 0.7,
    "惊讶": 0.6
}

class EmotionAnalyzer:
    """情感分析器类"""
    
    def __init__(self, model_path=None):
        """
        初始化情感分析器
        
        Args:
            model_path: 模型路径
        """
        self.model = None
        self.tokenizer = None
        
        if not _TORCH_AVAILABLE:
            print("[EmotionAnalysis] torch 不可用，情感分析器将以降级模式运行")
            self.device = None
            return
        
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        
        # 加载模型
        self._load_model(model_path)
    
    def _load_model(self, model_path=None):
        """
        加载情感分析模型
        
        Args:
            model_path: 模型路径
        """
        try:
            if model_path and os.path.exists(model_path):
                # 加载本地模型
                print(f"📦 加载本地模型: {model_path}")
                self.tokenizer = BertTokenizer.from_pretrained(model_path)
                self.model = BertForSequenceClassification.from_pretrained(model_path, num_labels=7)
            else:
                # 加载预训练模型
                print(f"📦 加载预训练模型: bert-base-chinese")
                # 使用Hugging Face的预训练中文情感分析模型
                self.tokenizer = BertTokenizer.from_pretrained("bert-base-chinese")
                self.model = BertForSequenceClassification.from_pretrained(
                    "bert-base-chinese", 
                    num_labels=7,
                    ignore_mismatched_sizes=True
                )
            
            # 移动模型到设备
            self.model.to(self.device)
            self.model.eval()
            print(f"✅ 模型加载完成，使用设备: {self.device}")
        except Exception as e:
            print(f"❌ 加载模型失败: {e}")
            self.model = None
            self.tokenizer = None
    
    def analyze_emotion(self, text):
        """
        分析文本的情感
        
        Args:
            text: 要分析的文本
            
        Returns:
            dict: 情感分析结果
        """
        if not self.model or not self.tokenizer:
            return {
                "emotion": "中立",
                "score": 0.0,
                "confidence": 0.0,
                "intensity": EMOTION_INTENSITY["中立"]
            }
        
        try:
            # 预处理文本
            inputs = self.tokenizer(
                text,
                padding="max_length",
                truncation=True,
                max_length=128,
                return_tensors="pt"
            )
            
            # 移动到设备
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            
            # 模型推理
            with torch.no_grad():
                outputs = self.model(**inputs)
                logits = outputs.logits
                probabilities = torch.nn.functional.softmax(logits, dim=-1)
                
            # 获取结果
            predicted_class = torch.argmax(probabilities, dim=-1).item()
            emotion = EMOTION_CLASSES.get(predicted_class, "中立")
            confidence = probabilities[0][predicted_class].item()
            intensity = EMOTION_INTENSITY.get(emotion, 0.5)
            
            # 获取所有类别的概率
            scores = {}
            for i, prob in enumerate(probabilities[0]):
                scores[EMOTION_CLASSES.get(i, f"类别{i}")] = prob.item()
            
            return {
                "emotion": emotion,
                "score": scores,
                "confidence": confidence,
                "intensity": intensity
            }
        except Exception as e:
            print(f"❌ 情感分析失败: {e}")
            return {
                "emotion": "中立",
                "score": 0.0,
                "confidence": 0.0,
                "intensity": EMOTION_INTENSITY["中立"]
            }
    
    def get_response_strategy(self, emotion):
        """
        根据情感获取响应策略
        
        Args:
            emotion: 情感类别
            
        Returns:
            dict: 响应策略
        """
        strategies = {
            "愤怒": {
                "tone": "calm",
                "speed": "slow",
                "content_type": "comforting",
                "response_style": "sympathetic",
                "suggestion": "用户当前情绪愤怒，建议采用平静、缓慢的语气回应，提供安慰和解决方案。"
            },
            "厌恶": {
                "tone": "neutral",
                "speed": "normal",
                "content_type": "objective",
                "response_style": "respectful",
                "suggestion": "用户当前情绪厌恶，建议保持中立、客观的语气，尊重用户的感受，避免引起进一步的厌恶。"
            },
            "恐惧": {
                "tone": "reassuring",
                "speed": "slow",
                "content_type": "calming",
                "response_style": "supportive",
                "suggestion": "用户当前情绪恐惧，建议采用安慰、缓慢的语气，提供支持和解决方案，帮助用户减轻恐惧。"
            },
            "快乐": {
                "tone": "enthusiastic",
                "speed": "normal",
                "content_type": "positive",
                "response_style": "cheerful",
                "suggestion": "用户当前情绪快乐，建议采用热情、积极的语气回应，保持愉快的交流氛围。"
            },
            "中立": {
                "tone": "neutral",
                "speed": "normal",
                "content_type": "informative",
                "response_style": "professional",
                "suggestion": "用户当前情绪中立，建议采用中立、专业的语气，提供准确、有用的信息。"
            },
            "悲伤": {
                "tone": "sympathetic",
                "speed": "slow",
                "content_type": "comforting",
                "response_style": "supportive",
                "suggestion": "用户当前情绪悲伤，建议采用同情、缓慢的语气，提供安慰和支持。"
            },
            "惊讶": {
                "tone": "curious",
                "speed": "normal",
                "content_type": "explanatory",
                "response_style": "informative",
                "suggestion": "用户当前情绪惊讶，建议采用好奇、解释性的语气，提供详细的信息和解释。"
            }
        }
        
        return strategies.get(emotion, strategies["中立"])
    
    def adjust_response(self, text, emotion_result):
        """
        根据情感调整响应
        
        Args:
            text: 原始响应文本
            emotion_result: 情感分析结果
            
        Returns:
            str: 调整后的响应文本
        """
        emotion = emotion_result["emotion"]
        intensity = emotion_result["intensity"]
        
        # 根据情感调整响应
        if emotion == "愤怒":
            # 愤怒情绪，添加安抚语句
            adjusted_text = f"我理解您现在的感受，让我来帮助您解决这个问题：{text}"
        elif emotion == "悲伤":
            # 悲伤情绪，添加安慰语句
            adjusted_text = f"听到这个消息我很遗憾，{text}"
        elif emotion == "快乐":
            # 快乐情绪，添加积极语句
            adjusted_text = f"很高兴听到您这么说！{text}"
        elif emotion == "恐惧":
            # 恐惧情绪，添加安抚语句
            adjusted_text = f"别担心，{text}"
        else:
            # 其他情绪，保持原文本
            adjusted_text = text
        
        # 根据情感强度调整响应长度
        if intensity > 0.8:
            # 高强度情绪，响应更详细
            adjusted_text += " 如果您需要进一步的帮助，请随时告诉我。"
        elif intensity < 0.3:
            # 低强度情绪，响应更简洁
            adjusted_text = adjusted_text.split("。")[0] + "。"
        
        return adjusted_text

# 全局情感分析器实例
_emotion_analyzer = None

def get_emotion_analyzer():
    """
    获取全局情感分析器实例
    
    Returns:
        EmotionAnalyzer: 情感分析器实例
    """
    global _emotion_analyzer
    if _emotion_analyzer is None:
        _emotion_analyzer = EmotionAnalyzer()
    return _emotion_analyzer

if __name__ == "__main__":
    # 测试情感分析
    analyzer = EmotionAnalyzer()
    
    test_texts = [
        "今天天气真好，心情愉快！",
        "这个产品质量太差了，我非常生气！",
        "我很害怕一个人晚上出门。",
        "这个消息让我感到很悲伤。",
        "这件事真的让我感到很惊讶！",
        "我对这个决定感到很厌恶。",
        "今天的会议很顺利。"
    ]
    
    for text in test_texts:
        result = analyzer.analyze_emotion(text)
        print(f"\n文本: {text}")
        print(f"情感: {result['emotion']}")
        print(f"置信度: {result['confidence']:.2f}")
        print(f"强度: {result['intensity']:.2f}")
        
        # 获取响应策略
        strategy = analyzer.get_response_strategy(result['emotion'])
        print(f"响应策略: {strategy['tone']}")
        print(f"建议: {strategy['suggestion']}")
        
        # 测试响应调整
        original_response = "这是一个标准的响应。"
        adjusted_response = analyzer.adjust_response(original_response, result)
        print(f"调整后的响应: {adjusted_response}")