from patchguru.utils.ClonedRepoManager import ClonedRepoManager
from patchguru.utils.PullRequest import PullRequest
from github import Github, Auth
import sys
import traceback


def get_repo(project_name):
    if project_name == "pandas":
        cloned_repo_manager = ClonedRepoManager(
            "../clones", "pandas", "pandas-dev/pandas", "pandas-dev", "pandas")
    elif project_name == "scikit-learn":
        cloned_repo_manager = ClonedRepoManager(
            "../clones", "scikit-learn", "scikit-learn/scikit-learn", "scikit-learn-dev", "sklearn")
    elif project_name == "scipy":
        cloned_repo_manager = ClonedRepoManager(
            "../clones", "scipy", "scipy/scipy", "scipy-dev", "scipy")
    elif project_name == "numpy":
        cloned_repo_manager = ClonedRepoManager(
            "../clones", "numpy", "numpy/numpy", "numpy-dev", "numpy")
    elif project_name == "transformers":
        cloned_repo_manager = ClonedRepoManager(
            "../clones", "transformers", "huggingface/transformers", "transformers-dev", "transformers")
    elif project_name == "keras":
        cloned_repo_manager = ClonedRepoManager(
            "../clones", "keras", "keras-team/keras", "keras-dev", "keras")
    elif project_name == "marshmallow":
        cloned_repo_manager = ClonedRepoManager(
            "../clones", "marshmallow", "marshmallow-code/marshmallow", "marshmallow-dev", "marshmallow")
    elif project_name == "pytorch_geometric":
        cloned_repo_manager = ClonedRepoManager(
            "../clones", "pytorch_geometric", "pyg-team/pytorch_geometric", "pytorch_geometric-dev", "torch_geometric")
    elif project_name == "scapy":
        cloned_repo_manager = ClonedRepoManager(
            "../clones", "scapy", "secdev/scapy", "scapy-dev", "scapy")
    else:
        raise ValueError(f"Project {project_name} is not supported.")

    # NOTE: no `git pull` here. Under the dual-loader pool the clones are
    # commit-pinned (detached-HEAD) checkouts, and `git pull` on a detached
    # HEAD fails ("You are not currently on a branch"), surfacing as a
    # spurious `retrieval_error`. Retrieval only needs the GitHub repo object.
    token = open(".github_token", "r").read().strip()
    github = Github(auth=Auth.Token(token))
    github_repo = github.get_repo(cloned_repo_manager.repo_id)

    return github_repo, cloned_repo_manager

def retrieve_pr(project, pr_nb):
    try:
        github_repo, cloned_repo_manager = get_repo(project)
        github_pr = github_repo.get_pull(pr_nb)
        pr = PullRequest(github_pr, github_repo, cloned_repo_manager)
    except Exception as exc:
        # Surface the cause so the failure is visible in batch runs; the
        # caller (analyze) records a `retrieval_error` failed state.
        print(
            f"[PRRetriever] Failed to retrieve {project} PR #{pr_nb}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return None, None, None
    return pr, cloned_repo_manager, github_repo

if __name__ == "__main__":
    project = "pandas"  # Change this to the desired project
    pr_nb = 62101  # Change this to the desired PR number
    pr, cloned_repo_manager, github_repo = retrieve_pr(project, pr_nb)
