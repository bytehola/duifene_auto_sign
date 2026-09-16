/* 对分易自动签到 — 前端逻辑
 * 与 Python 的交互只走 pywebview js_api；日志经轮询 drain_logs 拉取。 */

(function () {
  "use strict";

  var api = null;
  var logCursor = 0;
  var logTotal = 0;
  var autoScroll = true;
  var pollTimer = null;
  var clockTimer = null;
  // 运行态与课程可用性：二者共同决定课程下拉与「开始监听」的可用性，
  // 由 syncRunControls 统一推导，避免多处各写 disabled 而互相覆盖。
  var watching = false;
  var hasCourses = false;
  // 开始/停止请求在途：期间控件保持禁用，避免连点重复提交。
  var runBusy = false;

  // 微信授权链接（复制按钮与说明步骤共用；此处为原始文本，& 不做 HTML 转义）
  var WECHAT_LINK =
    "https://open.weixin.qq.com/connect/oauth2/authorize?appid=wx1b5650884f657981&redirect_uri=https://www.duifene.com/_FileManage/PdfView.aspx?file=https%3A%2F%2Ffs.duifene.com%2Fres%2Fr2%2Fu6106199%2F%E5%AF%B9%E5%88%86%E6%98%93%E7%99%BB%E5%BD%95_876c9d439ca68ead389c.pdf&response_type=code&snsapi_userinfo&connect_redirect=1#wechat_redirect";

  // 说明步骤：字符串为普通文案；{link:true} 处渲染「链接 + 复制按钮」。
  // 注意：不能用 ``step.link`` 判断——字符串经原型链装箱会命中已废弃的
  // String.prototype.link 方法（返回函数，truthy），导致所有文本步骤都被误判。
  var WECHAT_HELP = [
    "打开电脑端微信，复制如下链接到文件传输助手并发送：",
    { isLink: true },
    "点击进入链接，点击微信浏览器窗口右上角三个点，点击复制链接。",
    "把微信链接粘贴到左侧「登录链接」输入框，点击登录。",
  ];

  var $ = function (id) {
    return document.getElementById(id);
  };

  // 设置字段定义：与后端 PERSISTABLE_FIELDS 对齐（标签与分组属表现层）。
  var SETTINGS_GROUPS = [
    {
      title: "轮询与退避",
      fields: [
        {
          key: "poll_min_s",
          label: "轮询最小间隔",
          hint: "秒",
          type: "number",
        },
        {
          key: "poll_max_s",
          label: "轮询最大间隔",
          hint: "秒",
          type: "number",
        },
        {
          key: "login_check_interval_s",
          label: "登录态检查间隔",
          hint: "秒",
          type: "number",
        },
        {
          key: "error_backoff_max_s",
          label: "异常退避上限",
          hint: "秒",
          type: "number",
        },
      ],
    },
    {
      title: "监听时间窗",
      fields: [
        {
          key: "monitor_start_hour",
          label: "起始小时",
          hint: "0–24，含",
          type: "number",
        },
        {
          key: "monitor_end_hour",
          label: "结束小时",
          hint: "0–24，不含",
          type: "number",
        },
      ],
    },
    {
      title: "签到门禁",
      fields: [
        {
          key: "small_class_max",
          label: "小班人数阈值",
          hint: "低于此值直接签到",
          type: "number",
        },
        {
          key: "small_class_delay_s",
          label: "小班等待时长",
          hint: "秒",
          type: "number",
        },
        {
          key: "headcount_ratio",
          label: "大班签到占比",
          hint: "0–1，如 0.2",
          type: "number",
        },
        {
          key: "stale_sign_minutes",
          label: "旧活动上限",
          hint: "分钟，超过则跳过",
          type: "number",
        },
      ],
    },
    {
      title: "定位与推送",
      fields: [
        {
          key: "ws_follow_distance_m",
          label: "推送跟随距离",
          hint: "米，超过则不跟随",
          type: "number",
        },
        {
          key: "ws_wait_timeout_s",
          label: "推送等待上限",
          hint: "秒",
          type: "number",
        },
      ],
    },
    {
      title: "限流与网络",
      fields: [
        {
          key: "cooldown_seconds",
          label: "限流静默时长",
          hint: "秒",
          type: "number",
        },
        {
          key: "proxy_url",
          label: "代理地址",
          hint: "留空为直连；如 http://127.0.0.1:8080",
          type: "text",
        },
      ],
    },
  ];

  var settingsValues = {};
  var settingsDefaults = {};

  function fmtTime(ts) {
    var d = new Date(ts * 1000);
    var p = function (n) {
      return (n < 10 ? "0" : "") + n;
    };
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function toast(text, kind) {
    var el = $("toast");
    el.textContent = text;
    el.className = "toast show" + (kind ? " " + kind : "");
    clearTimeout(el._t);
    el._t = setTimeout(function () {
      el.className = "toast";
    }, 2600);
  }

  // 模态提示：用于登录态失效等必须被用户确认的消息。
  // 背景遮罩同时变暗并拦截点击，点「确定」后关闭。
  function showModal(text, title) {
    var mask = $("modalMask");
    if (!mask) return;
    $("modalTitle").textContent = title || "提示";
    $("modalText").textContent = text;
    mask.classList.add("open");
    mask.setAttribute("aria-hidden", "false");
    $("modalOk").focus();
  }

  function hideModal() {
    var mask = $("modalMask");
    if (!mask) return;
    mask.classList.remove("open");
    mask.setAttribute("aria-hidden", "true");
  }

  // 登录尝试（点击登录按钮）的结果一律用 toast：这是用户主动操作后的即时反馈，
  // 不该弹窗打断。弹窗只留给后台察觉「既有会话失效」这类必须确认的状态事件
  // （见 showNotice / bootstrap）——两种语义不同，不能共用同一通道。
  function notifyResult(res) {
    if (!res) return;
    var msg = res.message || "";
    if (msg) toast(msg, res.ok ? "success" : "error");
  }

  function markInvalid(input) {
    input.classList.add("invalid");
    setTimeout(function () {
      input.classList.remove("invalid");
    }, 1200);
  }

  function setLoading(btnId, spinnerId, loading) {
    var btn = $(btnId),
      sp = $(spinnerId);
    if (btn) btn.disabled = loading;
    if (sp) sp.classList.toggle("hidden", !loading);
  }

  // 课程下拉与「开始监听」的可用性统一由此推导，单一来源避免冲突：
  //   - 监听中：两者禁用（锁定本次监听的课程，防止中途换课造成错配）；
  //   - 未登录（无课程）：两者禁用；
  //   - 请求在途：两者禁用，避免连点重复提交；
  //   - 已停止且有课程：两者启用。
  function syncRunControls() {
    var startBtn = $("startBtn");
    var sel = $("courseSelect");
    var disabled = !hasCourses || watching || runBusy;
    if (startBtn) startBtn.disabled = disabled;
    if (sel) sel.disabled = disabled;
  }

  // ─ 日志渲染 ────────────────────────────────────────────
  function appendLogs(items) {
    if (!items || !items.length) return;
    var body = $("logBody");
    var empty = $("logEmpty");
    if (empty) empty.remove();
    var frag = document.createDocumentFragment();
    items.forEach(function (it) {
      var line = document.createElement("div");
      line.className = "log-line " + (it.level || "info");
      var t = document.createElement("span");
      t.className = "log-time";
      t.textContent = fmtTime(it.ts);
      var x = document.createElement("span");
      x.className = "log-text";
      x.textContent = it.text;
      line.appendChild(t);
      line.appendChild(x);
      frag.appendChild(line);
    });
    body.appendChild(frag);
    logTotal += items.length;
    var lc = $("logCount");
    if (lc) lc.textContent = logTotal + " 条";
    if (autoScroll) body.scrollTop = body.scrollHeight;
  }

  function clearLogs() {
    logTotal = 0;
    var lc = $("logCount");
    if (lc) lc.textContent = "0 条";
    $("logBody").innerHTML =
      '<div class="log-empty" id="logEmpty">' +
      '<div class="log-empty-title">暂无日志</div>' +
      '<div class="log-empty-sub">登录并开始监听后将在此显示</div></div>';
  }

  // 登录成功后重置日志视图：清空列表并把游标复位到后端返回的日志序号。
  // 后端在登录成功时已清空日志队列，故这里只需复位本地显示与游标；
  // 不能只清列表而不动游标——留旧游标会让后续按 id 拉取的增量出现空洞。
  function resetLogsAfterLogin(nextId) {
    clearLogs();
    if (typeof nextId === "number") logCursor = nextId;
  }

  // ─ 状态刷新 ────────────────────────────────────────────
  // 登录状态位：由后端 logged_in 驱动（权威判定），未登录 / 已登录两态。
  function setLoginChip(loggedIn) {
    var chip = $("loginChip");
    if (!chip) return;
    loggedIn = !!loggedIn;
    chip.className = "chip chip-login " + (loggedIn ? "on" : "off");
    $("loginText").textContent = loggedIn ? "已登录" : "未登录";
  }

  function applyState(st) {
    var pill = $("statusPill");
    var running = !!(st && st.running);
    watching = running;
    pill.className = "status-pill " + (running ? "running" : "stopped");
    $("statusText").textContent = running ? "运行中" : "已停止";
    $("stopBtn").disabled = !running;
    syncRunControls();

    if (st) {
      $("monCourse").textContent = st.course_name || "—";
      $("lastTick").textContent = st.last_tick_ts
        ? fmtTime(st.last_tick_ts)
        : "—";
      // 限流倒计时：>0 时显示「还需 N 秒」，否则隐藏该条目。
      var left = Number(st.limiter_remaining_s) || 0;
      var item = $("limiterItem");
      if (item) {
        if (left > 0) {
          $("limiterLeft").textContent = Math.ceil(left) + " 秒";
          item.classList.remove("hidden");
        } else {
          item.classList.add("hidden");
        }
      }
      // logged_in 只在后端状态里出现；start/stop 的部分状态调用
      // （如 applyState({running:true})）不含该字段，不可据此改状态位。
      if (typeof st.logged_in === "boolean") {
        setLoginChip(st.logged_in);
        // 后台探测到会话失效时，后端已清空当前课程；此处同步复位课程
        // 选择器，否则会留着失效课程的旧选项且「开始监听」仍可点。
        if (!st.logged_in && $("courseSelect").value) {
          fillCourses([], "");
        }
      }
      if (st.notice) showNotice(st.notice);
    }
  }

  // 实时时钟：每秒本地跳动，体现"程序在运行"
  function tickClock() {
    var el = $("clockTime");
    if (el) el.textContent = fmtTime(Date.now() / 1000);
  }

  function showNotice(notice) {
    var b = $("noticeBanner");
    if (!b) return;
    // 登录态失效用模态确认（横幅易被忽略），其余仍走横幅。
    if (String(notice.text || "").indexOf("重新登录") !== -1) {
      showModal(notice.text, "登录失败");
      b.classList.add("hidden");
      return;
    }
    b.className = "notice-banner " + (notice.level || "error");
    b.textContent = notice.text;
    b.classList.remove("hidden");
  }

  function fillCourses(courses, selected) {
    var sel = $("courseSelect");
    sel.innerHTML = "";
    hasCourses = !!(courses && courses.length);
    if (!hasCourses) {
      sel.innerHTML = '<option value="">请先登录</option>';
      syncRunControls();
      return;
    }
    courses.forEach(function (c) {
      var o = document.createElement("option");
      o.value = c.id;
      o.textContent = c.name;
      if (String(c.id) === String(selected)) o.selected = true;
      sel.appendChild(o);
    });
    syncRunControls();
  }

  // ─ 轮询 ────────────────────────────────────────────────
  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      api.drain_logs(logCursor).then(function (r) {
        if (!r) return;
        logCursor = r.next_id;
        appendLogs(r.items);
      });
      api.get_state().then(applyState);
    }, 400);
  }

  // ─ 初始化 ──────────────────────────────────────────────
  function bootstrap() {
    api.bootstrap().then(function (res) {
      if (!res) return;
      $("versionChip").textContent = "v" + res.version;
      document.title = "对分易自动签到 v" + res.version;
      fillCourses(res.courses, res.selected);
      setLoginChip(res.logged_in);
      if (res.logs && res.logs.length) {
        logCursor = res.logs[res.logs.length - 1].id;
        appendLogs(res.logs);
      }
      if (res.message) {
        // 登录态失效属必须被用户明确确认的消息：弹窗提示，而非一闪而过的 toast。
        if (res.message.indexOf("重新登录") !== -1) {
          showModal(res.message, "登录失败");
        } else {
          toast(res.message, "error");
        }
      }
      startPolling();
    });
  }

  // ─ 事件绑定 ────────────────────────────────────────────
  // 登录页签高度取两者较高者，使「微信链接 / 账号密码」切换时面板尺寸不变。
  // 用 min-height 而非叠层或写死像素：页签仍按 display 正常切换、互不重叠，
  // 故不会在 WebView2 合成层留下残影；高度又不会随页签长短跳动。
  function lockTabPaneHeight() {
    var link = $("paneLink"),
      pwd = $("panePwd");
    if (!link || !pwd) return;
    var linkHidden = link.classList.contains("hidden"),
      pwdHidden = pwd.classList.contains("hidden");
    // 量高度需元素可见：先临时显示，量完还原。
    link.classList.remove("hidden");
    pwd.classList.remove("hidden");
    // 用 boundingRect 并向上取整：offsetHeight 会取整到整数，与亚像素布局
    // 叠加后可能比另一个页签少 1px，导致切换时高度抖动。
    var tallest = Math.ceil(
      Math.max(
        link.getBoundingClientRect().height,
        pwd.getBoundingClientRect().height,
      ),
    );
    if (linkHidden) link.classList.add("hidden");
    if (pwdHidden) pwd.classList.add("hidden");
    link.style.minHeight = tallest + "px";
    pwd.style.minHeight = tallest + "px";
  }

  function bind() {
    // 分段选项卡
    $("loginTabs").addEventListener("click", function (e) {
      var btn = e.target.closest(".seg");
      if (!btn) return;
      Array.prototype.forEach.call(
        document.querySelectorAll(".seg"),
        function (b) {
          b.classList.toggle("active", b === btn);
        },
      );
      var isLink = btn.dataset.tab === "link";
      $("paneLink").classList.toggle("hidden", !isLink);
      $("panePwd").classList.toggle("hidden", isLink);
    });

    // 链接登录
    $("loginLinkBtn").addEventListener("click", function () {
      var val = $("linkInput").value.trim();
      if (val.indexOf("code=") === -1) {
        markInvalid($("linkInput"));
        toast("链接有误", "error");
        return;
      }
      setLoading("loginLinkBtn", "linkSpinner", true);
      api.login_by_link(val).then(function (r) {
        setLoading("loginLinkBtn", "linkSpinner", false);
        notifyResult(r);
        if (r.ok) {
          resetLogsAfterLogin(r.next_id);
          api.bootstrap().then(function (b) {
            fillCourses(b.courses, b.selected);
            setLoginChip(b.logged_in);
          });
        }
      });
    });

    // 密码登录
    $("loginPwdBtn").addEventListener("click", function () {
      var u = $("userInput").value.trim();
      if (!u) {
        markInvalid($("userInput"));
        toast("请输入账号", "error");
        return;
      }
      setLoading("loginPwdBtn", "pwdSpinner", true);
      api.login_by_password(u, $("passInput").value).then(function (r) {
        setLoading("loginPwdBtn", "pwdSpinner", false);
        notifyResult(r);
        if (r.ok) {
          resetLogsAfterLogin(r.next_id);
          api.bootstrap().then(function (b) {
            fillCourses(b.courses, b.selected);
            setLoginChip(b.logged_in);
          });
        }
      });
    });

    // 课程切换
    $("courseSelect").addEventListener("change", function () {
      if (!this.value) return;
      api.select_course(this.value).then(function (r) {
        if (!r.ok) toast(r.message, "error");
      });
    });

    // 开始监听
    $("startBtn").addEventListener("click", function () {
      runBusy = true;
      syncRunControls();
      $("startSpinner").classList.remove("hidden");
      api.start_watching().then(function (r) {
        runBusy = false;
        $("startSpinner").classList.add("hidden");
        toast(r.message, r.ok ? "success" : "error");
        if (r.ok) applyState({ running: true });
        else syncRunControls();
      });
    });

    // 停止监听
    $("stopBtn").addEventListener("click", function () {
      api.stop_watching().then(function (r) {
        toast(r.message, r.ok ? "success" : "error");
        applyState({ running: false });
      });
    });

    // 自动滚动开关
    $("autoScrollBtn").addEventListener("click", function () {
      autoScroll = !autoScroll;
      this.classList.toggle("active", autoScroll);
      if (autoScroll) $("logBody").scrollTop = $("logBody").scrollHeight;
    });

    // 手动清空：同时清前端显示与后端队列，否则旧日志会在下次 bootstrap
    // 时被重新拉回。
    $("clearBtn").addEventListener("click", function () {
      clearLogs();
      api.clear_logs().then(function (r) {
        if (r && r.ok) logCursor = r.next_id;
      });
    });

    // 遮罩仅用于关闭设置抽屉
    $("overlay").addEventListener("click", closeSettings);

    // 模态提示：点「确定」或遮罩关闭；Esc 亦可关闭
    $("modalOk").addEventListener("click", hideModal);
    $("modalMask").addEventListener("click", function (e) {
      if (e.target === this) hideModal();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") hideModal();
    });

    // 关窗时确保停止监控
    window.addEventListener("beforeunload", function () {
      if (api) api.stop_watching();
    });

    // 自绘窗口按钮（无边框窗口）
    $("winMin").addEventListener("click", function () {
      if (api && api.window_minimize) api.window_minimize();
    });
    $("winClose").addEventListener("click", function () {
      if (api && api.window_close) api.window_close();
    });

    // 顶栏是可拖区域（.pywebview-drag-region）；其内的交互控件须阻止
    // mousedown 冒泡，否则点按钮会被 pywebview 判定为拖窗。
    Array.prototype.forEach.call(
      document.querySelectorAll(
        ".topbar button, .topbar select, .topbar input",
      ),
      function (el) {
        el.addEventListener("mousedown", function (e) {
          e.stopPropagation();
        });
      },
    );

    // 设置抽屉
    $("settingsBtn").addEventListener("click", openSettings);
    $("settingsClose").addEventListener("click", closeSettings);
    $("settingsSave").addEventListener("click", saveSettings);
    $("settingsReset").addEventListener("click", function () {
      renderSettings(settingsDefaults);
      toast("已填入默认值，点击保存生效");
    });
  }

  // 复制文本：优先 Clipboard API（WebView2 下 file:// 属可信来源可用），
  // 失败则回退到 execCommand 兼容路径。
  function copyText(text) {
    var fallback = function () {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-1000px";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (e) {
        ok = false;
      }
      document.body.removeChild(ta);
      return ok;
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(
        function () {
          return true;
        },
        function () {
          return fallback();
        },
      );
    }
    return Promise.resolve(fallback());
  }

  function copyWechatLink() {
    copyText(WECHAT_LINK).then(function (ok) {
      toast(
        ok ? "链接已复制" : "复制失败，请手动选取",
        ok ? "success" : "error",
      );
    });
  }

  // 填充帮助步骤
  function renderHelp() {
    var ol = $("helpSteps");
    ol.innerHTML = "";
    var lastLi = null;
    WECHAT_HELP.forEach(function (step) {
      // 链接行不单独占一个列表项：否则 <ol> 会自动编号，出现「2.」并导致后续断号。
      // 把它并入上一条步骤的 li 内，编号仍为 1 / 2 / 3。
      if (step && typeof step === "object" && step.isLink) {
        if (!lastLi) return;
        var row = document.createElement("div");
        row.className = "help-link-row";
        var code = document.createElement("code");
        code.className = "help-link";
        code.textContent = WECHAT_LINK;
        code.title = WECHAT_LINK;
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-primary btn-sm help-copy-btn";
        btn.textContent = "复制链接";
        btn.addEventListener("click", copyWechatLink);
        row.appendChild(code);
        row.appendChild(btn);
        lastLi.appendChild(row);
        return;
      }
      var li = document.createElement("li");
      li.textContent = step;
      ol.appendChild(li);
      lastLi = li;
    });
  }

  // ─ 设置抽屉 ────────────────────────────────────────────
  function fieldInputId(key) {
    return "set_" + key;
  }

  function buildSettingsForm() {
    var form = $("settingsForm");
    form.innerHTML = "";
    SETTINGS_GROUPS.forEach(function (group) {
      var box = document.createElement("div");
      box.className = "settings-group";
      var title = document.createElement("h3");
      title.className = "settings-group-title";
      title.textContent = group.title;
      box.appendChild(title);

      group.fields.forEach(function (f) {
        var row = document.createElement("div");
        row.className = "settings-row";

        var text = document.createElement("div");
        text.className = "settings-row-text";
        var label = document.createElement("div");
        label.className = "settings-row-label";
        label.textContent = f.label;
        var hint = document.createElement("div");
        hint.className = "settings-row-hint";
        hint.textContent = f.hint || "";
        text.appendChild(label);
        text.appendChild(hint);

        var ctrl = document.createElement("div");
        ctrl.className = "settings-row-control";
        var inputId = fieldInputId(f.key);
        if (f.type === "bool") {
          var sw = document.createElement("label");
          sw.className = "switch";
          sw.innerHTML =
            '<input type="checkbox" id="' +
            inputId +
            '"><span class="track"></span>';
          ctrl.appendChild(sw);
        } else {
          var input = document.createElement("input");
          input.className = "input";
          input.id = inputId;
          // 数值字段也用 text：后端负责类型转换与范围校验，
          // number 的原生步进控件样式与整体表单不协调。
          input.type = "text";
          if (f.placeholder) input.placeholder = f.placeholder;
          ctrl.appendChild(input);
        }

        row.appendChild(text);
        row.appendChild(ctrl);
        box.appendChild(row);
      });
      form.appendChild(box);
    });
  }

  function renderSettings(values) {
    SETTINGS_GROUPS.forEach(function (group) {
      group.fields.forEach(function (f) {
        var el = $(fieldInputId(f.key));
        if (!el) return;
        var v = values[f.key];
        if (f.type === "bool") el.checked = !!v;
        else el.value = v === undefined || v === null ? "" : String(v);
        el.classList.remove("invalid");
      });
    });
  }

  function collectSettings() {
    var patch = {};
    SETTINGS_GROUPS.forEach(function (group) {
      group.fields.forEach(function (f) {
        var el = $(fieldInputId(f.key));
        if (!el) return;
        patch[f.key] = f.type === "bool" ? el.checked : el.value.trim();
      });
    });
    return patch;
  }

  function openSettings() {
    api.get_settings().then(function (r) {
      if (!r || !r.ok) {
        toast(r && r.message ? r.message : "读取配置失败", "error");
        return;
      }
      if (!Object.keys(settingsDefaults).length)
        settingsDefaults = Object.assign({}, r.values);
      settingsValues = r.values;
      renderSettings(settingsValues);
      $("settingsDrawer").classList.add("open");
      $("overlay").classList.add("open");
    });
  }

  function closeSettings() {
    $("settingsDrawer").classList.remove("open");
    $("overlay").classList.remove("open");
  }

  function saveSettings() {
    var patch = collectSettings();
    api.update_settings(patch).then(function (r) {
      if (!r || !r.ok) {
        toast(r && r.message ? r.message : "保存失败", "error");
        return;
      }
      settingsValues = r.values;
      renderSettings(settingsValues);
      toast("设置已保存", "success");
      closeSettings();
    });
  }

  // 等待 pywebview 注入 js_api
  window.addEventListener("pywebviewready", function () {
    api = window.pywebview.api;
    renderHelp();
    buildSettingsForm();
    bind();
    lockTabPaneHeight();
    tickClock();
    clockTimer = setInterval(tickClock, 1000);
    bootstrap();
  });
})();
