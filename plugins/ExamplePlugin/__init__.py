"""示例插件包入口：必须导出 plugin.yaml 里 entry 指定的类。"""
from .main import ExamplePlugin

__all__ = ["ExamplePlugin"]
