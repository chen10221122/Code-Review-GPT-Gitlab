import importlib
import pkgutil
from utils.logger import log



class ReviewHandle(object):
    def __init__(self):
        pass

    def merge_handle(self, gitlabMergeRequestFetcher, gitlabRepoManager, hook_info, reply, model):
        log.info("="*50)
        log.info("开始处理新的 Merge Request")
        log.info(f"""
        项目名称: {hook_info['project']['name']}
        源分支: {hook_info['object_attributes']['source_branch']}
        目标分支: {hook_info['object_attributes']['target_branch']}
        提交者: {hook_info['user']['name']}
        """)
        log.info("-"*50)
        pass