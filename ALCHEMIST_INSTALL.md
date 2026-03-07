# ALCHEMIST ComfyUI 安装指南

本文档描述如何从源代码安装和运行 ALCHEMIST ComfyUI 项目。

## 项目结构

本项目由以下6个 Git 仓库组成：

1. **主仓库 (ALCHEMIST)** - ComfyUI 后端主仓库
2. **frontend-new** - ComfyUI 前端仓库
3. **custom_nodes/ALCHEM_job_master** - Job Master 自定义节点
4. **custom_nodes/ALCHEM_PropBtn** - 分子处理和3D可视化节点
5. **custom_nodes/NODE_ALCHEM_Dock** - 分子对接节点
6. **custom_nodes/ALCHEM_PropBtn/nodes/process/NODE_bfee_dock** - BFEE Docking 节点
7. **custom_nodes/project_manager** - 项目管理插件

## 系统要求

- **操作系统**: Linux / WSL2 (Windows Subsystem for Linux) / macOS
- **Python**: 3.11
- **Node.js**: >= 24
- **Conda**: Miniconda 或 Anaconda
- **CUDA**: 12.1 或更高 (如需 GPU 加速)
- **Git**: 用于克隆仓库

## 安装步骤

### 1. 克隆主仓库

```bash
cd ~/Desktop/soft_dev
git clone <your-ALCHEMIST-repo-url> ALCHEMIST
cd ALCHEMIST
```

注意：本项目包含多个子仓库（git submodules），确保也克隆了所有子仓库：

```bash
# 如果使用 submodules
git submodule update --init --recursive
```

### 2. 接受 Conda 服务条款 (首次安装)

```bash
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
```

### 3. 创建 Conda 环境

创建名为 `comfyui` 的 Python 3.11 环境：

```bash
conda create -n comfyui python=3.11 -y
conda activate comfyui
```

### 4. 应用后端 Patch

ALCHEM_job_master 提供了一个 patch 用于扩展 ComfyUI 后端的 hook 功能：

```bash
# 注意：patch 已由安装脚本自动应用，如果手动安装失败，请检查 server.py 是否已包含 PromptSubmitContext
# 如需重新应用：
# git apply custom_nodes/ALCHEM_job_master/patches/comfyui-hook-update.patch
```

该 patch 添加了以下功能：
- Prompt 提交拦截 hook
- History provider hook
- 用于 custom nodes 的高级集成能力

### 5. 安装主仓库依赖

#### 5.1 安装 PyTorch (GPU 版本)

```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
```

如果需要 CPU 版本：

```bash
pip install torch torchvision torchaudio
```

#### 5.2 安装其他 Python 依赖

```bash
pip install -r requirements.txt
```

### 6. 安装 Custom Nodes 依赖

#### 6.1 ALCHEM_PropBtn

```bash
pip install -r custom_nodes/ALCHEM_PropBtn/requirements.txt
```

主要依赖：rdkit, pandas, aiohttp, websockets

#### 6.2 NODE_ALCHEM_Dock

```bash
pip install -r custom_nodes/NODE_ALCHEM_Dock/requirements.txt
pip install gemmi  # meeko 的额外依赖
```

主要依赖：pytorch-lightning, lightning, torch-geometric, fair-esm, biopython, meeko, wandb, gemmi

#### 6.3 project_manager

```bash
pip install -r custom_nodes/project_manager/requirements.txt
```

主要依赖：aiofiles, pytest, pytest-asyncio, pydantic, watchdog

#### 6.4 NODE_bfee_dock (Conda 依赖)

NODE_bfee_dock 需要多个生物信息学工具，使用 conda 安装：

```bash
conda install -c conda-forge biotite biopython dimorphite-dl mdanalysis numba \
    openbabel pdb-tools pdb2pqr pyside6 smina vina qvina pymol-open-source -y
```

#### 6.5 下载前端可视化库 (Tabulator, Molstar, Ketcher)

ALCHEM_PropBtn 节点需要以下前端库：
- **Tabulator**: CSV 表格展示与编辑
- **Molstar**: 3D 分子可视化
- **Ketcher**: 2D 分子编辑器

##### 下载 Tabulator (CSV 表格库)

```bash
# 创建目录
mkdir -p custom_nodes/ALCHEM_PropBtn/web/csv/lib

# 下载 tabulator.min.js (433KB)
curl -o custom_nodes/ALCHEM_PropBtn/web/csv/lib/tabulator.min.js \
    https://cdnjs.cloudflare.com/ajax/libs/tabulator-tables/6.3.1/js/tabulator.min.js

# 下载 tabulator.min.css (28KB)
curl -o custom_nodes/ALCHEM_PropBtn/web/csv/lib/tabulator.min.css \
    https://cdnjs.cloudflare.com/ajax/libs/tabulator-tables/6.3.1/css/tabulator.min.css
```

##### 下载 Molstar (3D 分子可视化)

```bash
# 下载 molstar.js (4.6MB)
curl -o custom_nodes/ALCHEM_PropBtn/web/molstar/lib/molstar.js \
    https://unpkg.com/molstar@latest/build/viewer/molstar.js

# 下载 molstar.css (72KB)
curl -o custom_nodes/ALCHEM_PropBtn/web/molstar/lib/molstar.css \
    https://unpkg.com/molstar@latest/build/viewer/molstar.css
```

##### 下载 Ketcher 3.7.0

```bash
# 下载 Ketcher standalone 包 (20.2MB)
curl -L -o /tmp/ketcher-standalone.zip \
    https://github.com/epam/ketcher/releases/download/v3.7.0/ketcher-standalone-3.7.0.zip

# 解压到临时目录
python3 -c "import zipfile; zipfile.ZipFile('/tmp/ketcher-standalone.zip').extractall('/tmp/ketcher-extracted')"

# 复制所有文件到 lib 目录
cp -r /tmp/ketcher-extracted/standalone/* custom_nodes/ALCHEM_PropBtn/web/ketcher/lib/

# 清理临时文件
rm -rf /tmp/ketcher-standalone.zip /tmp/ketcher-extracted
```

验证文件已正确下载：

```bash
ls -lh custom_nodes/ALCHEM_PropBtn/web/csv/lib/
# 应该看到: tabulator.min.js (433K), tabulator.min.css (28K)

ls -lh custom_nodes/ALCHEM_PropBtn/web/molstar/lib/
# 应该看到: molstar.js (4.6M), molstar.css (72K)

ls -lh custom_nodes/ALCHEM_PropBtn/web/ketcher/lib/
# 应该看到: index.html, static/ 目录, 以及其他 Ketcher 文件
```

#### 6.6 构建 project_manager Vue 前端

project_manager 插件包含一个 Vue.js 前端，需要单独构建：

```bash
cd custom_nodes/project_manager/web
npm install
npm run build
cd ../../..
```

构建完成后会在 `custom_nodes/project_manager/web/dist/` 目录生成 `main.js` 文件。

### 7. 构建主前端

#### 7.1 安装 pnpm

```bash
npm install -g pnpm
```

#### 7.2 进入前端目录并安装依赖

```bash
cd frontend-new
pnpm install
```

#### 7.3 临时修复 nx.json (移除 playwright 插件)

编辑 `frontend-new/nx.json`，删除或注释掉 `@nx/playwright/plugin` 配置：

```json
{
  "$schema": "./node_modules/nx/schemas/nx-schema.json",
  "plugins": [
    {
      "plugin": "@nx/eslint/plugin",
      "options": {
        "targetName": "lint"
      }
    },
    {
      "plugin": "@nx/storybook/plugin",
      "options": {
        "serveStorybookTargetName": "storybook",
        "buildStorybookTargetName": "build-storybook",
        "testStorybookTargetName": "test-storybook",
        "staticStorybookTargetName": "static-storybook"
      }
    },
    {
      "plugin": "@nx/vite/plugin",
      "options": {
        "buildTargetName": "build",
        "testTargetName": "test",
        "serveTargetName": "serve",
        "devTargetName": "dev",
        "previewTargetName": "preview",
        "serveStaticTargetName": "serve-static",
        "typecheckTargetName": "typecheck",
        "buildDepsTargetName": "build-deps",
        "watchDepsTargetName": "watch-deps"
      }
    }
    // 已移除 playwright 插件以避免构建错误
  ]
}
```

#### 7.4 构建前端

```bash
pnpm nx build
```

构建完成后会在 `frontend-new/dist` 目录生成前端静态文件。

#### 7.5 返回主目录

```bash
cd ..
```

## 运行 ComfyUI

### 启动后端服务器

确保 conda 环境已激活，并使用正确的前端路径：

```bash
conda activate comfyui
python main.py --front-end-root ./frontend-new/dist/
```

常用启动参数：

```bash
# 监听所有网络接口（允许外部访问）
python main.py --front-end-root ./frontend-new/dist/ --listen 0.0.0.0

# 指定端口
python main.py --front-end-root ./frontend-new/dist/ --port 8188

# 启用 CUDA（GPU 加速）
python main.py --front-end-root ./frontend-new/dist/ --gpu-only

# CPU 模式
python main.py --front-end-root ./frontend-new/dist/ --cpu

# 查看所有可用参数
python main.py --help
```

**重要**：必须使用 `--front-end-root ./frontend-new/dist/` 参数来指定前端静态文件路径。

### 访问 Web 界面

启动成功后，访问：

```
http://localhost:8188
```

如果使用 `--listen 0.0.0.0`，可以通过其他设备访问：

```
http://<你的IP地址>:8188
```

## 开发模式

### 前端开发

如果需要修改前端代码，可以使用开发服务器：

```bash
cd frontend-new
pnpm dev
```

开发服务器会在 `http://localhost:5173` 启动，并自动连接到后端。

### 后端开发

修改后端代码后，重启 `main.py` 即可。

## 故障排除

### 1. NumPy 版本冲突

如果遇到 NumPy 版本相关错误（例如："A module that was compiled using NumPy 1.x cannot be run in NumPy 2.x"），降级到 NumPy 1.x：

```bash
conda activate comfyui
pip install "numpy<2" scipy --force-reinstall
```

这是因为 conda 安装的某些生物信息学包是用 NumPy 1.x 编译的。

### 2. Conda 环境问题

如果遇到 conda 环境相关错误，确保：

```bash
conda activate comfyui
which python  # 应该指向 ~/miniconda3/envs/comfyui/bin/python
```

### 3. 缺失模块（如 gemmi）

如果看到类似 "ModuleNotFoundError: No module named 'gemmi'" 的错误：

```bash
conda activate comfyui
pip install gemmi
```

### 4. CUDA 相关错误

如果 GPU 不可用，检查：

```bash
python -c "import torch; print(torch.cuda.is_available())"
```

如果返回 `False`，可以：
- 使用 CPU 模式：`python main.py --cpu`
- 检查 CUDA 驱动和 toolkit 版本

### 5. 前端构建失败

如果前端构建失败：

1. 确保 Node.js >= 24
2. 清理缓存并重新安装：
   ```bash
   cd frontend-new
   rm -rf node_modules pnpm-lock.yaml
   pnpm install
   pnpm nx build
   ```

### 6. Custom Nodes 未加载

检查：

```bash
# 确保所有 custom_nodes 目录都有 __init__.py
ls -la custom_nodes/*/

# 查看 ComfyUI 启动日志，确认节点加载情况
```

### 7. 依赖冲突

如果遇到 Python 包版本冲突，可以：

```bash
# 重新创建环境
conda deactivate
conda env remove -n comfyui
# 然后重新执行步骤 3-6
```

## 更新

### 更新主仓库和子模块

```bash
git pull
git submodule update --remote --merge
```

### 更新依赖

```bash
conda activate comfyui
pip install -r requirements.txt --upgrade
cd frontend-new && pnpm install && cd ..
```

### 重新构建前端

```bash
cd frontend-new
pnpm nx build
cd ..
```

## 卸载

如果需要完全卸载：

```bash
# 删除 conda 环境
conda deactivate
conda env remove -n comfyui

# 删除项目目录
rm -rf ~/Desktop/soft_dev/ALCHEMIST

# 卸载全局 pnpm（可选）
npm uninstall -g pnpm
```

## 许可证

请参考各个仓库的 LICENSE 文件。

## 支持

如遇问题，请查看各个仓库的文档：

- ComfyUI 主仓库: README.md
- 前端文档: frontend-new/README.md
- Custom nodes 文档: custom_nodes/*/README.md

---

**安装日期**: 2026-01-14
**文档版本**: 1.0
