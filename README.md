# 游戏 Linux 兼容性

Miyu 脚本包，带一件工具 `game_compat`：查一款游戏在 Linux 上能不能玩，三个来源并排交回。

| 来源 | 给什么 |
|---|---|
| ProtonDB | 评级（总评、最近、最好）、报告数、最近几份玩家报告：结论、Proton 版本、出的问题、启动参数、备注 |
| AreWeAntiCheatYet | 反作弊状态（Supported、Denied……）、用的是哪家反作弊、最近的变动 |
| caniplayonlinux | 一句话结论和最后核实的日期 |

| 参数 | 说明 |
|---|---|
| `query` | Steam App ID 或游戏名，必填。英文原名最准；中文名照 Steam 的中文商店名对 |
| `max_reports` | 列几份 ProtonDB 玩家报告，0–20，默认 6；0 只给评级 |
| `format` | `md`（默认）给人和 AI 读；`json` 给代码用，比如 `miyu call` 接 `jq`、Python。出错也是 JSON：`{"ok": false, "error": …}` |

- 先认游戏再查：App ID 经 Steam 查名字；英文名经 ProtonDB 的搜索索引；中文名先找 Steam 中文商店名完全对得上的，再看搜索索引给的，英文结果要和那款游戏的中文商店名对得上才算。对不上的不硬查，交回搜到了什么、让换英文原名，不会把另一款游戏的数据当成这款。
- 不在 Steam 上的游戏（无畏契约、英雄联盟这类）没有 ProtonDB，但反作弊和 caniplayonlinux 照名字还能查到，用英文名查。
- 某个来源没收录写「没收录」，出错写「出错」，两样不混：没收录不等于没有反作弊。
- AreWeAntiCheatYet 的总表和 caniplayonlinux 的站点地图缓存一天，放在 `MIYU_SCRIPT_CACHE_DIR`。
- 只用 Python 标准库。
