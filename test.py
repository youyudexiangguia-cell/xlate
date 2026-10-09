import sys
import numpy as np

# 1. 输出当前正在运行的 Python 解释器绝对路径
print(f"当前运行解释器: {sys.executable}")

# 2. 验证路径是否位于当前项目的 .venv 目录中
if ".venv" in sys.executable:
    print("✅ 解释器路径正确：已自动命中当前项目 .venv 环境")
else:
    print("⚠️ 警告：当前未运行在 .venv 环境中，请检查 VS Code 解释器配置！")

# 3. 基础 NumPy 运算测试
arr = np.array([1, 2, 3, 4, 5])
matrix = np.eye(3)

print(f"NumPy 版本: {np.__version__}")
print("1D 数组测试:", arr * 2)
print("单位矩阵对角线元素和:", int(np.trace(matrix)))
print("✅ NumPy 基础计算与加载正常！")