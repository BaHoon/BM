# Server Setup Guide

这份文档用于把本仓库的 `scripts/` 交给队友后，在 Linux 服务器上跑通 Android benchmark pipeline。

核心结论：服务器不仅需要 Python 依赖，还需要 Android 构建和 UI 自动化环境。也就是说，服务器上要安装 JDK、Android SDK、Node.js/Appium，并且要有可用的 Android 设备或模拟器。

## 1. 简略目录结构

```text
BM/
├── requirements.txt             (github上面已上传)
├── SERVER_SETUP.md              (github上面已上传)
├── scripts/                     (github上面已上传)
│   ├── Experiment_Launcher.py   # 批量调度入口
│   ├── Env_Manager.py           # workspace 重置、标准答案回放、Gradle 编译
│   ├── agent_runner.py          # LLM 代码生成与 ReAct/tool_planning 工具执行
│   ├── appium_runner.py         # Appium 启动、APK 安装、UI 测试、截图/UI tree 采集
│   ├── Evaluator.py             # Level 1/2/3 评分、VSM/CSR 计算
│   ├── llm_client.py            # OpenAI-compatible LLM API 封装
│   ├── retriever.py             # RAG 文件检索
│   └── logger.py                # 实验日志与 raw_data 输出
├── data/                        （打包到飞书上面了）# 本地数据，不进 Git；队友运行时必须提供
│   ├── app_foodyou/
│   │   ├── base_src/
│   │   ├── task_001_theme/
│   │   │   ├── meta.json
│   │   │   ├── test_script.py
│   │   │   └── ground_truth_src/
│   │   └── ...
│   └── TODO1_output/
│       └── *_golden_mapping.json
├── workspace/                   # 运行时生成，不进 Git
└── results/                     # 运行结果，不进 Git
```

如果只打包 `scripts/` 给队友是不够的。至少还要给：

```text
requirements.txt
data/<app>/base_src/
data/<app>/<task>/meta.json
data/<app>/<task>/test_script.py
data/<app>/<task>/ground_truth_src/       # 如果要跑标准答案 oracle 模式
data/TODO1_output/*_golden_mapping.json   # 如果要用文件 overlap fallback 评分
```

`workspace/` 和 `results/` 不需要提前给，程序会自动生成。

## 2. 服务器系统依赖

下面以 Ubuntu/Debian 服务器为例。

```bash
sudo apt-get update
sudo apt-get install -y \
  git curl unzip zip wget \
  python3 python3-venv python3-pip \
  openjdk-21-jdk \
  nodejs npm
```

JDK版本要装Java 21，不要装17。

验证：

```bash
java -version
javac -version
node -v
npm -v
```

## 3. Python 环境

在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` 主要包含：

```text
openai
Appium-Python-Client
numpy
scikit-learn
pandas
openpyxl
```

## 4. Android SDK

服务器上必须有 Android SDK，否则 Gradle 会报：

```text
SDK location not found.
Define a valid SDK location with an ANDROID_HOME environment variable
or by setting the sdk.dir path in local.properties
```

ai推荐安装到：

```text
$HOME/android-sdk
```

安装方式：

1. 从 Android Developers 官网下载 `Command line tools only` 的 Linux zip 包。
2. 解压到 `$HOME/android-sdk/cmdline-tools/latest/`。
3. 使用 `sdkmanager` 安装平台、build-tools、platform-tools。

目录最终应该长这样：

```text
$HOME/android-sdk/
├── cmdline-tools/latest/bin/sdkmanager
├── platform-tools/adb
├── platforms/android-36/
└── build-tools/...
```

配置环境变量：

```bash
cat >> ~/.bashrc <<'EOF'
export ANDROID_HOME="$HOME/android-sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$ANDROID_HOME/platform-tools:$ANDROID_HOME/cmdline-tools/latest/bin:$PATH"
EOF
source ~/.bashrc
```

安装需要的 SDK 包：

```bash
sdkmanager --licenses
sdkmanager \
  "platform-tools" \
  "cmdline-tools;latest" \
  "build-tools;36.0.0" \
  "platforms;android-36" \
  "emulator"
```

当前数据里的 FoodYou 和 NewsReader 都使用 `compileSdk = 36`，所以至少需要 `platforms;android-36`。如果以后换数据集，按对应项目里的 `compileSdk` 安装相应平台。

验证：

```bash
echo "$ANDROID_HOME"
adb version
sdkmanager --list | grep 'platforms;android-36'
```

如果 Gradle 仍找不到 SDK，可以在具体 workspace 项目根目录写一个 `local.properties`：

```properties
sdk.dir=/home/<user>/android-sdk
```

一般配置 `ANDROID_HOME`/`ANDROID_SDK_ROOT` 就够了。

## 5. Appium 环境

安装 Appium 2 和 UiAutomator2 driver：

```bash
sudo npm install -g appium
appium driver install uiautomator2
```

可选安装检查工具：

```bash
sudo npm install -g appium-doctor
appium-doctor --android
```

手动启动 Appium：

```bash
appium --host localhost --port 4723 --allow-insecure adb_screen_recording
```

本代码的 `AppiumRunner` 也会在检测不到服务时尝试自动启动 Appium。

## 6. Android 设备或模拟器

Appium 需要一台可用 Android 设备。服务器有三种常见方案：

1. 服务器连接真机，并打开 USB debugging。
2. 服务器跑 Android Emulator，需要 KVM/硬件虚拟化支持。
3. 连接远程 Android 设备或云真机。

最低验证标准：

```bash
adb devices
```

输出里必须有一台状态为 `device` 的设备，例如：

```text
List of devices attached
emulator-5554    device
```

注意：`scripts/appium_runner.py` 里现在的默认设备名是：

```python
ADB_DEVICE = "127.0.0.1:7555"
```

这更像本地模拟器端口。如果服务器上用的是 `emulator-5554` 或真机序列号，建议把这里改成实际设备名，或者后续把它改成环境变量读取。

## 7. LLM API 配置

这个先不用管，后续会加。

## 8. 运行方式

### 8.1 先用标准答案测试评分流程

这个模式跳过 LLM 生成代码，直接把 `data/<app>/<task>/ground_truth_src` 复制到 workspace，然后完整运行编译、Appium、Evaluator。

```bash
python scripts/Experiment_Launcher.py \
  --use-ground-truth \
  --model oracle \
  --strategy direct \
  --tasks app_foodyou/task_005_notice
```

如果标准答案都不能高分，优先检查：

```text
results/<app>/<task>/compilation_result.json
results/<app>/<task>/compile_stdout.txt
results/<app>/<task>/compile_stderr.txt
results/<app>/<task>/appium_result.json
results/<app>/<task>/eval_result.json
```

## 9. 常见错误排查

### SDK location not found

原因：没有安装 Android SDK，或 `ANDROID_HOME`/`ANDROID_SDK_ROOT` 没配置。

检查：

```bash
echo "$ANDROID_HOME"
ls "$ANDROID_HOME/platform-tools/adb"
```

### ADB device not available

原因：没有设备、设备 offline、服务器没权限访问 USB、模拟器没启动。

检查：

```bash
adb kill-server
adb start-server
adb devices
```

### Appium server not running

检查：

```bash
appium --version
appium driver list --installed
curl http://localhost:4723/status
```

### Could not find platform android-36

安装缺失平台：

```bash
sdkmanager "platforms;android-36" "build-tools;36.0.0"
```

### Gradle 下载很慢或失败

第一次构建会下载 Gradle distribution 和 Android/Gradle 插件。服务器需要能访问：

```text
services.gradle.org
repo.maven.apache.org
dl.google.com
plugins.gradle.org
```

如果服务器无法直连，需要配置代理或提前缓存 Gradle/Maven 依赖。

## 10. 给队友打包建议

建议不要只发 `scripts/`，而是发一个最小运行包：

```text
BM-runtime/
├── requirements.txt
├── SERVER_SETUP.md
├── scripts/
└── data/
    ├── app_foodyou/
    ├── app_newsreader/
    └── TODO1_output/
```

不要打包：

```text
workspace/
results/
scripts/__pycache__/
*.pyc
.env
```

如果必须压缩发送：

```bash
zip -r BM-runtime.zip requirements.txt SERVER_SETUP.md scripts data \
  -x '*/__pycache__/*' '*.pyc' '.env' 'workspace/*' 'results/*'
```
