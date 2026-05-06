"""
通用配置加载模块
"""
import yaml
import logging
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)


def load_config(config_path: str = None) -> Dict[str, Any]:
    """
    加载配置文件

    Args:
        config_path: 配置文件路径，默认为项目根目录的 config.yaml

    Returns:
        配置字典
    """
    if config_path is None:
        # 默认查找项目根目录的 config.yaml
        config_path = Path(__file__).parent.parent / "config.yaml"

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}\n"
            "请复制 config.yaml.example 为 config.yaml 并填入配置"
        )

    with open(path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    return config


def setup_logging(log_file: str = None, level: int = logging.INFO):
    """
    配置日志

    Args:
        log_file: 日志文件路径
        level: 日志级别
    """
    handlers = [logging.StreamHandler()]

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding='utf-8'))

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers
    )

    # 降低第三方库日志级别
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
