# agent-hub

Để Claude, Codex, Antigravity (và agent khác) cùng làm trên một repo mà **không ghi đè code của nhau**, và mỗi agent làm phần mình giỏi nhất.
Chỉ dùng Python chuẩn, không cần cài gì thêm.

## Cách nó chống mâu thuẫn
| Vấn đề | Cách xử lý |
|---|---|
| Hai agent sửa cùng một file | Mỗi task khoá một phạm vi file (`-f 'src/api/**'`). Claim trùng phạm vi sẽ bị từ chối. |
| Agent "tiện tay" sửa file của người khác | Claude: hook PreToolUse chặn ngay lúc Edit/Write. Mọi agent: git pre-commit chặn khi commit. |
| Làm trước khi phần phụ thuộc xong | `--after T1`: chưa xong T1 thì không claim được. |
| Code chồng code trong cùng thư mục | `-w` tạo git worktree + branch riêng `hub/<agent>/<task>` cho mỗi agent. |
| Tự review code của mình | Bị cấm: `done` chuyển task sang agent khác review. |
| Mất ngữ cảnh khi chuyển tay | `note` + `prompt` tạo brief đầy đủ cho agent tiếp theo. |

## Phân vai mặc định (sửa trong `.agent-hub.json`)
- **claude**: lên kế hoạch, kiến trúc, refactor, debug khó, review, docs
- **codex**: implement task đã rõ, viết test, sửa lỗi, script/CLI
- **antigravity**: UI/frontend, kiểm tra trên trình duyệt, đọc codebase lớn

Thêm agent mới (ví dụ `gemini`, `cursor`): thêm một mục vào `agents` với `strengths` và `cmd` (để trống nếu là IDE, dùng `hub prompt` rồi dán vào).

## Bắt đầu
```bash
alias hub="python3 ~/dev/tools/agent-hub/hub.py"   # thêm vào ~/.zshrc
cd my-project && hub init
```
`init` tạo/cập nhật `AGENTS.md` (Codex và Antigravity đọc), `CLAUDE.md` và `GEMINI.md` (trỏ về AGENTS.md), `.agent-hub.json`, hook của Claude và git pre-commit.

## Luồng làm việc điển hình
```bash
# 1. Claude lên kế hoạch: chia task có phạm vi file không chồng nhau
hub add "Định nghĩa API contract" -f src/api/types.ts -r plan
hub add "Implement API"  -f 'src/api/**' -r impl --after T1
hub add "Làm UI"          -f 'src/ui/**'  -r ui   --after T1

# 2. Chạy song song, mỗi agent một worktree
hub run T2 --as codex -w          # tự claim + gọi `codex exec` kèm brief
hub prompt T3 --as antigravity    # in brief để dán vào Antigravity
hub next --as antigravity --claim -w

# 3. Xong -> agent khác review
hub done T2 --as codex -m "đã thêm 3 endpoint, chạy npm test"
hub run T2 --as claude            # Claude nhận brief review
hub review T2 --as claude --approve -m "ok"   # hoặc --changes -m "..."

hub status        # xem bảng
```

Trạng thái lưu trong `.git/hub/state.json`, nên mọi worktree của repo đều thấy cùng một bảng.
Đặt `"strict": true` để bắt agent chỉ được sửa file thuộc task mình đã claim.
