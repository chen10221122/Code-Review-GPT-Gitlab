import concurrent.futures
import threading

from retrying import retry
from config.config import GPT_MESSAGE, EXCLUDE_FILE_TYPES, IGNORE_FILE_TYPES, MAX_FILES
from review_engine.abstract_handler import ReviewHandle
from utils.gitlab_parser import filter_diff_content
from utils.logger import log


def chat_review(changes, generate_review, *args, **kwargs):
    log.info("开始code review")
    with concurrent.futures.ThreadPoolExecutor() as executor:
        review_results = []
        result_lock = threading.Lock()

        def process_change(change):
            result = generate_review(change, *args, **kwargs)
            with result_lock:
                review_results.append(result)

        futures = []
        for change in changes:
            if any(
                change["new_path"].endswith(ext) for ext in EXCLUDE_FILE_TYPES
            ) and not any(
                change["new_path"].endswith(ext) for ext in IGNORE_FILE_TYPES
            ):
                futures.append(executor.submit(process_change, change))
            else:
                log.info(f"{change['new_path']} 非目标检测文件！")

        concurrent.futures.wait(futures)

    return "\n\n".join(review_results) if review_results else ""


@retry(stop_max_attempt_number=3, wait_fixed=60000)
def generate_review_note(change, model):
    try:

        log.info("\n📝 开始审查文件")
        log.info(
            f"""
        文件路径: {change['new_path']}
        变更类型: {'新文件' if change['new_file'] else '修改'}
        """
        )

        content = filter_diff_content(change["diff"])
        log.info(f"处理文件：{change['new_path']}")

        messages = [
            {"role": "system", "content": GPT_MESSAGE},
            {
                "role": "user",
                "content": f"请review这部分代码变更{content}",
            },
        ]
        log.info("\n🤖 发送给模型的消息:")
        log.info(f"发送给gpt 内容如下：{messages}")

        # 调用模型
        model.generate_text(messages)
        new_path = change["new_path"]
        log.info(f"对 {new_path} review中...")

        # 获取模型返回内容并清理
        response_content = model.get_respond_content().replace("\n\n", "\n")
        # 移除 think 标签及其内容
        response_content = remove_think_content(response_content)

        log.info(f"模型返回内容：\n{response_content}")
        total_tokens = model.get_respond_tokens()
        review_note = f"# 📚`{new_path}`" + "\n\n"
        review_note += f'({total_tokens} tokens) {"AI review 意见如下:"}' + "\n\n"
        review_note += response_content + "\n\n---\n\n---\n\n"
        log.info(f"对 {new_path} review结束")
        return review_note
    except Exception as e:
        log.error(f"GPT error:{e}")


def remove_think_content(content):
    """移除 think 标签及其内容"""
    import re

    # 移除 <think>...</think> 块
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    return content.strip()


class MainReviewHandle(ReviewHandle):
    def merge_handle(
        self, gitlabMergeRequestFetcher, gitlabRepoManager, hook_info, reply, model
    ):

        # 添加MR信息日志
        log.info(
            f"开始处理MR，项目：{hook_info['project']['name']}, "
            f"源分支：{hook_info['object_attributes']['source_branch']}, "
            f"目标分支：{hook_info['object_attributes']['target_branch']}"
        )

        changes = gitlabMergeRequestFetcher.get_changes()

        # 添加变更文件数量日志
        log.info(f"检测到 {len(changes)} 个文件变更")

        log.info(
            f"""
变更文件信息:
- 文件总数: {len(changes) if changes else 0}
- 文件列表:"""
        )

        if changes:
            for change in changes:
                log.info(
                    f"""
    - 文件: {change['new_path']}
    - 类型: {'需要审查' if any(change["new_path"].endswith(ext) for ext in EXCLUDE_FILE_TYPES) else '不需要审查'}
    - 状态: {'忽略' if any(change["new_path"].endswith(ext) for ext in IGNORE_FILE_TYPES) else '正常'}
    """
                )

        merge_info = gitlabMergeRequestFetcher.get_info()
        self.default_handle(changes, merge_info, hook_info, reply, model)

    def default_handle(self, changes, merge_info, hook_info, reply, model):

        log.info(f"处理状态检查:")
        if not changes:
            log.info("❌ 没有检测到文件变更")
            return

        if len(changes) > MAX_FILES:
            log.info(f"❌ 文件数量 ({len(changes)}) 超过最大限制 ({MAX_FILES})")
            return

        # 检查是否有需要审查的文件
        review_files = [
            change
            for change in changes
            if any(change["new_path"].endswith(ext) for ext in EXCLUDE_FILE_TYPES)
            and not any(change["new_path"].endswith(ext) for ext in IGNORE_FILE_TYPES)
        ]

        if not review_files:
            log.info("❌ 没有需要审查的文件类型")
            return

        log.info(
            f"""
✅ 审查条件检查通过:
- 文件总数: {len(changes)} <= {MAX_FILES}
- 需要审查的文件数: {len(review_files)}
"""
        )

        if changes and len(changes) <= MAX_FILES:

            # 添加开始审查日志
            log.info(f"开始审查，共 {len(changes)} 个文件")

            # Code Review 信息
            review_info = chat_review(changes, generate_review_note, model)
            if review_info:

                # 添加审查完成日志
                log.info("代码审查完成，开始发送评论")
                reply.add_reply(
                    {
                        "content": review_info,
                        "msg_type": "MAIN, SINGLE",
                        "target": "all",
                    }
                )
                # 添加评论发送日志
                log.info("评论发送完成")

                reply.add_reply(
                    {
                        "title": "__MAIN_REVIEW__",
                        "content": (
                            f"## 项目名称: **{hook_info['project']['name']}**\n\n"
                            f"### 合并请求详情\n"
                            f"- **MR URL**: [查看合并请求]({hook_info['object_attributes']['url']})\n"
                            f"- **源分支**: `{hook_info['object_attributes']['source_branch']}`\n"
                            f"- **目标分支**: `{hook_info['object_attributes']['target_branch']}`\n\n"
                            f"### 变更详情\n"
                            f"- **修改文件个数**: `{len(changes)}`\n"
                            f"- **Code Review 状态**: ✅\n"
                        ),
                        "target": "dingtalk",
                        "msg_type": "MAIN, SINGLE",
                    }
                )
            else:
                reply.add_reply(
                    {
                        "title": "__MAIN_REVIEW__",
                        "content": (
                            f"## 项目名称: **{hook_info['project']['name']}**\n\n"
                            f"### 合并请求详情\n"
                            f"- **MR URL**: [查看合并请求]({hook_info['object_attributes']['url']})\n"
                            f"- **源分支**: `{hook_info['object_attributes']['source_branch']}`\n"
                            f"- **目标分支**: `{hook_info['object_attributes']['target_branch']}`\n\n"
                            f"### 变更详情\n"
                            f"- **修改文件个数**: `{len(changes)}`\n"
                            f"- **备注**: 存在已经提交的 MR，所有文件已进行 MR\n"
                            f"- **Code Review 状态**: pass ✅\n"
                        ),
                        "target": "dingtalk",
                        "msg_type": "MAIN, SINGLE",
                    }
                )

        elif changes and len(changes) > MAX_FILES:
            reply.add_reply(
                {
                    "title": "__MAIN_REVIEW__",
                    "content": (
                        f"## 项目名称: **{hook_info['project']['name']}**\n\n"
                        f"### 备注\n"
                        f"修改 `{len(changes)}` 个文件 > 50 个文件，不进行 Code Review ⚠️\n\n"
                        f"### 合并请求详情\n"
                        f"- **MR URL**: [查看合并请求]({hook_info['object_attributes']['url']})\n"
                        f"- **源分支**: `{hook_info['object_attributes']['source_branch']}`\n"
                        f"- **目标分支**: `{hook_info['object_attributes']['target_branch']}`\n"
                    ),
                    "target": "dingtalk",
                    "msg_type": "MAIN, SINGLE",
                }
            )

        else:
            log.error(
                f"获取merge_request信息失败，project_id: {hook_info['project']['id']} |"
                f" merge_iid: {hook_info['object_attributes']['iid']}"
            )
