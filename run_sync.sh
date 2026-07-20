#!/bin/bash
# 每日/手动同步：VidFlow + 房源平台(PropertyAgent) + CRM → data.js
# 用法：./run_sync.sh [窗口天数，默认30] [auto]
#   auto = 定时兜底模式：当天已成功同步过则跳过（8:00/9:00/10:00 三档共用，保证每天只更新一次）
cd "$(dirname "$0")" || exit 1
PY="/Library/Frameworks/Python.framework/Versions/3.10/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
DAYS="${1:-30}"
MODE="${2:-}"
mkdir -p logs

# 兜底跳过：auto 模式下当天已有成功记录则不再跑（手动运行不受限）
if [ "$MODE" = "auto" ] && grep -q "$(date '+%Y-%m-%d') .*sync done (exit 0)" logs/sync.log 2>/dev/null; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [兜底] 今日已成功同步，跳过" >> logs/sync.log
  exit 0
fi
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') sync start (window ${DAYS}d) ====="
  # 瞬时网络/代理抖动(如8点代理未就绪)自动重试：最多3次，间隔120秒
  SYNC_EXIT=1
  for attempt in 1 2 3; do
    if "$PY" sync_data.py "$DAYS"; then
      SYNC_EXIT=0
      break
    fi
    SYNC_EXIT=$?
    echo "[重试] 第 ${attempt} 次失败(exit $SYNC_EXIT)$([ $attempt -lt 3 ] && echo '，120秒后重试')"
    [ $attempt -lt 3 ] && sleep 120
  done
  echo "===== $(date '+%Y-%m-%d %H:%M:%S') sync done (exit $SYNC_EXIT) ====="

  # 失败处理：弹系统通知；若 10 点档(最后兜底，或更晚补跑)仍失败 → 发飞书回执，今日数据未更新需人工介入
  if [ "$SYNC_EXIT" -ne 0 ]; then
    osascript -e 'display notification "重试3次仍失败，看板数据未更新，详见 logs/sync.log" with title "数据看板同步失败" sound name "Basso"' 2>/dev/null
    FEISHU_WEBHOOK=$(grep '^FEISHU_WEBHOOK=' .env 2>/dev/null | cut -d= -f2-)
    HOUR=$((10#$(date '+%H')))
    if [ "$MODE" = "auto" ] && [ "$HOUR" -ge 10 ] && [ -n "$FEISHU_WEBHOOK" ]; then
      curl -sS -m 15 -X POST -H 'Content-Type: application/json' \
        -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"⚠️ 数据看板同步失败\\n时间: $(date '+%Y-%m-%d %H:%M')（8/9/10点三档均失败，各重试3次）\\n影响: 今日看板数据未更新，页面仍显示昨日数据\\n处理: 检查 Mac 网络/代理后手动运行 ~/claude专用/okproperty_dashboard/run_sync.sh\\n日志: logs/sync.log\"}}" \
        "$FEISHU_WEBHOOK" >> logs/sync.log 2>&1
      echo "[通知] 已发飞书回执"
    fi
  fi

  # 同步成功 → 发布到 GitHub Pages（有变化才提交；推送失败不影响本地数据）
  # 生成文件先对齐远端再覆盖（last-writer-wins），避免多份克隆间的合并冲突
  if [ "$SYNC_EXIT" -eq 0 ] && [ -d site/.git ]; then
    git -C site fetch -q origin 2>/dev/null && git -C site reset -q --hard origin/main
    for f in data.js data.json index.html; do [ -f "$f" ] && cp "$f" site/; done
    git -C site add -A
    if ! git -C site diff --cached --quiet; then
      git -C site commit -q -m "data: $(date '+%Y-%m-%d %H:%M') 自动同步

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
      if git -C site push -q origin main; then
        echo "[发布] 已推送 GitHub Pages (origin)"
      else
        echo "[发布] origin push 失败(离线?)，下次同步时随提交一起重试"
      fi
    else
      echo "[发布] 数据无变化，跳过推送"
    fi

    # 双仓库同步②：partner-lab(OKdubai) 推 data-sync 分支（按约定不直接推 main）
    # push 失败自愈：每次都先对齐远端再覆盖最新文件，失败留到下次自动带上
    if [ -d partnerlab/.git ]; then
      git -C partnerlab fetch -q origin 2>/dev/null
      if git -C partnerlab rev-parse -q --verify origin/data-sync >/dev/null; then
        git -C partnerlab checkout -q -B data-sync origin/data-sync
      elif git -C partnerlab rev-parse -q --verify origin/feat/property-dashboard >/dev/null; then
        git -C partnerlab checkout -q -B data-sync origin/feat/property-dashboard
      else
        git -C partnerlab checkout -q -B data-sync origin/main
      fi
      mkdir -p partnerlab/property-dashboard
      for f in data.js data.json index.html; do [ -f "$f" ] && cp "$f" partnerlab/property-dashboard/; done
      git -C partnerlab add -A
      if ! git -C partnerlab diff --cached --quiet; then
        git -C partnerlab commit -q -m "data: $(date '+%Y-%m-%d %H:%M') 自动同步

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
        if git -C partnerlab push -q origin data-sync; then
          echo "[发布] 已同步 partner-lab (data-sync)"
        else
          echo "[发布] partner-lab push 失败，下次重试"
        fi
      fi
    fi
  fi
  echo ""
} >> logs/sync.log 2>&1
