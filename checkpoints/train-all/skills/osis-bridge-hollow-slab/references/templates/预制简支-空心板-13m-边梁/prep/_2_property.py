"""OSIS 命令流 PROPERTY 模块 — 几何属性(坐标系、收缩徐变特性、钢束线型等)"""

from __future__ import annotations

from pyosis.core.engine import OSISEngine

def build_property(engine: OSISEngine) -> None:
    # 创建样条曲线（便捷入口，内部转发到对应 create_* 方法）
    engine.geometry.create("钢束-1-N1", "ARC3D", "TENDON", 0.15, 0.0, -0.375, 0.0, 5.58812, 0.0, -0.66, 10.0, 7.35188, 0.0, -0.66, 10.0, 12.79, 0.0, -0.375, 0.0)

if __name__ == "__main__":
    from _0_engine import engine
    build_property(engine)
