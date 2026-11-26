# 🎓 对分易自动签到 v3.1

![Python](https://img.shields.io/badge/Python-3.12+-blue?logo=python&logoColor=white)
![pywebview](https://img.shields.io/badge/pywebview-%3E%3D5.0-green?logo=web&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-yellow)

![界面预览](assets/gui.png)


## 🚀 快速上手


### 运行环境

程序界面基于 WebView2 渲染。Windows 10 与 Windows 11 通常随系统自带；
若启动时提示缺少 WebView2，运行仓库根目录的 `WebView2Setup.exe` 安装即可。

### 安装与运行

```bash
# 1. 获取源码
git clone https://github.com/bytehola/duifene_auto_sign.git
cd duifene_auto_sign

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动程序
python main.py
```

### 打包发布（Windows）

```bash
# 在干净的虚拟环境中构建，避免无关依赖混入产物
python -m venv build_env
build_env\Scripts\pip install -r requirements.txt pyinstaller

# onedir 模式，产物在 dist\对分易自动签到\
build_env\Scripts\pyinstaller build.spec --noconfirm
```

---

## 📖 使用指南

### 启动流程

1. 在左栏「登录」面板填写对分易账号和密码。
2. 登录成功后，账号、密码和 App 会话会默认保存。
3. 右栏「监控课程」下拉列表选择课程，点击「开始监听」。

### 支持签到方式

| 签到类型 |
|---------|
| 签到码 (type=1) |
| 二维码 (type=2) |
| 定位签到 (type=3) |
| 汉字签到 (type=4) |

---

## 📄 开源协议

本项目基于 MIT License 发布。
