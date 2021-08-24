from os import getenv
from dotenv import load_dotenv
from lib.net.github.api import GitHubAPI
from lib.manager import Manager
from lib.net.carbonara import Carbon as _Carbon

load_dotenv()

Git: GitHubAPI = GitHubAPI((getenv('GITHUB_MAIN'), getenv('GITHUB_SECONDARY')), 'itsmewulf')
Mgr: Manager = Manager(Git)
Carbon: _Carbon = _Carbon(Git.ses)
