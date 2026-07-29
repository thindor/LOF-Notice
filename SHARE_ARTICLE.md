# 为了不错过下次六万块的套利机会，我用 AI 写了个 LOF 套利监测工具

> 前两篇聊了 LOF 套利的坑和我的三步核实法。这篇是纯干货：代码怎么跑、定时任务怎么设，小白也能照着操作。

---

## 先看效果

打开一个网页，就能看到全市场溢价超过 3% 的 LOF 基金。净值是核实过的，申购状态是实时查的，公告变动也会标出来。

暂停申购的、每天只能买 10 块的、真正能买的——一目了然。

![页面截图](截图待补充)

项目地址：**[https://github.com/thindor/LOF-Notice](https://github.com/thindor/LOF-Notice)**

---

## 零基础操作指南

下面分两种情况。如果你会用命令行就看 A，如果完全不懂技术就看 B。

### A. 会用命令行的（3 步）

```bash
# 1. 下载代码
git clone https://github.com/thindor/LOF-Notice.git
cd LOF-Notice

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动
python main.py
```

然后浏览器打开 `http://localhost:8877`，完事。

---

### B. 完全零基础（跟着截图走）

**第一步：下载 Python**

1. 打开 [python.org](https://www.python.org/downloads/)
2. 点那个黄色的大按钮「Download Python」
3. 下载完成后双击安装
4. **重要**：安装界面第一页，**勾选底部的「Add Python to PATH」**，然后再点 Install
5. 等待安装完成

**第二步：下载项目代码**

1. 打开 [https://github.com/thindor/LOF-Notice](https://github.com/thindor/LOF-Notice)
2. 点绿色的「Code」按钮 → 点「Download ZIP」
3. 下载后解压到桌面，会得到一个 `LOF-Notice` 文件夹

**第三步：安装依赖**

1. 按键盘 `Win + R`，输入 `cmd`，回车
2. 黑色窗口打开后，输入以下命令（一行一行来，每行输完按回车）：

```
cd Desktop\LOF-Notice
pip install -r requirements.txt
```

这一步会下载几个必要的库，等它跑完就行，大概需要一两分钟。

**第四步：启动**

还在那个黑色窗口里，输入：

```
python main.py
```

看到 `Uvicorn running on http://0.0.0.0:8877` 就说明成功了。

**第五步：打开网页**

浏览器地址栏输入 `http://localhost:8877`，回车。搞定。

---

## 每天 14:40 自动跑

想让电脑每天交易日下午自动刷新数据：

#### Windows

1. 键盘按 `Win`，搜索「任务计划程序」，打开
2. 右侧点「创建基本任务」
3. 名称填 `LOF监测`，下一步
4. 触发器选「每周」，勾选周一至周五，下一步
5. 时间填 `14:40`，下一步
6. 操作选「启动程序」，下一步
7. 程序填：`python`（如果提示找不到，就填 Python 安装路径，通常在 `C:\Users\你的用户名\AppData\Local\Programs\Python\Python3xx\python.exe`）
8. 参数填：`scheduler.py`
9. 起始于填：你解压项目的路径，比如 `C:\Users\你的用户名\Desktop\LOF-Notice`
10. 完成

#### Mac

打开终端，输入：

```bash
crontab -e
```

按 `i` 进入编辑模式，粘贴这行：

```
40 14 * * 1-5 cd ~/Desktop/LOF-Notice && python3 scheduler.py
```

按 `Esc`，输入 `:wq`，回车。

---

## 注意事项

- 如果你用的是公司电脑，网络走了代理，代码里已经处理好了。如果遇到网络错误，可以试试关掉代理再跑
- 不要在短时间内反复刷新，数据有 5 分钟缓存
- 收盘后（15:00 之后）溢价会收敛，数据变少是正常的
- 这个工具只帮你筛选，不替你下单。下单前自己去基金页面再核实一遍

---

代码在 **[github.com/thindor/LOF-Notice](https://github.com/thindor/LOF-Notice)**，觉得有用就点个 Star。有什么问题或者改进想法，直接提 Issue 或者评论区聊。

> 免责声明：仅供研究学习，不构成投资建议。投资有风险，操作需谨慎。
