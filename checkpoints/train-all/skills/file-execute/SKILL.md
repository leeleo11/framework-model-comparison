---
name: file-execute
description: 当用户要求执行上传的代码文件时使用。支持Python文件(.py)和OSIS命令文件(.sml/.out)的执行。
---

# 文件执行

上传的文件已被自动复制到系统临时目录，**无需手动创建或写入文件**。

## 查找临时目录

如需确认文件位置，可通过以下方式获取临时目录路径：

```bash
# Windows CMD
echo %TEMP%

# Windows PowerShell
$env:TEMP

# Python
python -c "import tempfile; print(tempfile.gettempdir())"
```

## Python文件执行

通过Python解释器执行:
```bash
python <file_path>
```

## SML/OUT文件执行

```bash
python -c "from pyosis import OSISEngine; engine = OSISEngine(); engine.import_apdl(<file_path>)"
```

