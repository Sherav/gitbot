import re
import discord
from typing import TYPE_CHECKING
from lib.utils.regex import GITHUB_LINES_URL_RE, GITLAB_LINES_URL_RE
from cogs.github.other.snippets._snippet_tools import get_text_from_url_and_data, compile_url

if TYPE_CHECKING:
    from lib.structs import GitBotContext


class GitHubLinesView(discord.ui.View):
    # children[0] = back button
    # children[1] = forward button
    # children[2] = revert button (return to cached "original message content", which is actually dynamically fetched)
    children: list['GitHubLinesButton', '_GitHubLinesBackToOriginalButton']
    __original_url__: str
    __original_l1__: int
    __original_l2__: int | None

    """
    View facilitating the viewing of consecutive lines of code from a GitHub line link.
    Meant to only be used in the raw text implementation.
    """

    def __init__(self, ctx: 'GitBotContext', lines_url: str, timeout: int = 180) -> None:
        # we use a lot of max(n, 1) calls to clamp linenos and prevent any shenanigans
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.lines_url: str = lines_url
        self.parsed: re.Match = (re.search(GITHUB_LINES_URL_RE, self.lines_url) or
                                 re.search(GITLAB_LINES_URL_RE, self.lines_url))
        self.platform: str = self.parsed.group('platform')
        self.l1: int = max(int(self.parsed.group('first_line_number')), 1)
        self.l2: int | None = self.ctx.bot.mgr.opt(self.parsed.groupdict().get('second_line_number'), int)
        self._set_originals()
        _fmt = ctx.l.views.button.github_lines.view_from_to.format  # save some chars
        _b_l1, _b_l2 = max(self.l1 - 25, 1), max(self.l1 - 1, 1)  # precomp backwards values
        self.add_item(GitHubLinesButton(forward=False,
                                        label=_fmt(_b_l1, _b_l2) if _b_l1 != _b_l2 else ctx.l.views.button.github_lines.view.format(1),
                                        emoji='⬅️', style=discord.ButtonStyle.gray))
        self.add_item(GitHubLinesButton(forward=True,
                                        label=_fmt(max((self.l2 or self.l1) + 1, 1), max((self.l2 or self.l1) + 25, 1)),
                                        emoji='➡️', style=discord.ButtonStyle.gray))
        self.add_item(_GitHubLinesBackToOriginalButton())
        self.children[2].disabled = True  # revert available only when lines are changed
        self.ctx.bot.logger.debug(f'Instantiated GitHubLinesView with url {self.lines_url} for MID {self.ctx.message.id}')

    def _set_originals(self) -> None:
        # required by the revert button at children[2]
        self.__original_url__: str = self.lines_url
        self.__original_l1__: int = self.l1
        self.__original_l2__: int | None = self.l2
        self.__original_match__: re.Match = self.parsed

    def set_labels(self, range_backward: tuple[int, int], range_forward: tuple[int, int]) -> None:
        # discord.py maintains child order
        # first is backwards, second is forwards
        if range_backward[0] == range_backward[1]:
            self.children[0].label = self.ctx.l.views.button.github_lines.view.format(1)
        else:
            self.children[0].label = self.ctx.l.views.button.github_lines.view_from_to.format(*range_backward)
        self.children[1].label = self.ctx.l.views.button.github_lines.view_from_to.format(*range_forward)
        self.ctx.bot.logger.debug(f'Backward label set to {self.children[0].label};'
                                  f' forward label set to {self.children[1].label} for MID {self.ctx.message.id}')


class GitHubLinesButton(discord.ui.Button):
    view: GitHubLinesView

    def __init__(self, forward: bool, **kwargs):
        super().__init__(**kwargs, custom_id=f'github_lines_button_{"forward" if forward else "backward"}')
        self.forward: bool = forward

    async def callback(self, interaction: discord.Interaction):
        self.view.children[2].disabled = False  # make reverting available
        ctx: 'GitBotContext' = self.view.ctx
        await interaction.response.defer()
        previous_l1, previous_l2 = self.view.l1, self.view.l2
        self.view.l1, self.view.l2 = self.get_next_lines(self.view.l1, self.view.l2, self.forward)
        ctx.bot.logger.debug(f'Previous lines: p_l1={previous_l1}, p_l2={previous_l2}')
        ctx.bot.logger.debug(
            f'Lines to display: l1={self.view.l1}, l2={self.view.l2} for MID {ctx.message.id}; forward={self.forward}')
        match self.view.platform:
            case 'github':
                if previous_l2 is None:
                    self.view.lines_url = self.view.lines_url.replace(f'#L{previous_l1}', f'#L{self.view.l1}-L{self.view.l2}')
                else:
                    self.view.lines_url = self.view.lines_url.replace(f'L{previous_l1}-L{previous_l2}', f'L{self.view.l1}-L{self.view.l2}')
            case 'gitlab':
                if previous_l2 is None:
                    self.view.lines_url = self.view.lines_url.replace(f'#L{[previous_l1]}', f'#L{self.view.l1}-{self.view.l2}')
                else:
                    self.view.lines_url = self.view.lines_url.replace(f'#L{previous_l1}-L{previous_l2}', f'#L{self.view.l1}-L{self.view.l2}')
        new_match = self.view.parsed.groups()[:4] + (self.view.l1, self.view.l2)
        new, _ = await get_text_from_url_and_data(ctx, compile_url(new_match), new_match)
        if new:
            l_b, l_f = self.get_next_lines(self.view.l1, self.view.l2, False), self.get_next_lines(self.view.l1, self.view.l2, True)
            self.view.set_labels(l_b, l_f)
            # TODO make the line numbers injected below into a hyperlink when the discord devs add support for them back
            await interaction.message.edit(
                content=f'`#L{self.view.l1}{f"-L{str(self.view.l2)}" if self.view.l2 != 1 else ""}`\n{new}',
                view=self.view,
            )

    @staticmethod
    def get_next_lines(l1: int, l2: int | None, forward: bool) -> tuple[int, int]:
        if forward:
            if l2 is not None:
                l1: int = l2 + 1
                l2 += 25
            else:
                l2: int = l1 + 25
        else:
            l2: int = l1 - 1
            l1 -= 25
        return max(l1, 1), max(l2, 1)


class _GitHubLinesBackToOriginalButton(discord.ui.Button):
    view: GitHubLinesView

    def __init__(self):
        super().__init__(custom_id='github_lines_back_to_original', style=discord.ButtonStyle.gray, emoji='↩️')

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.disabled = True  # disable revert button until lines that are displayed are changed again
        self.view.l1, self.view.l2 = self.view.__original_l1__, self.view.__original_l2__
        self.view._url = self.view.__original_url__
        self.view.set_labels(GitHubLinesButton.get_next_lines(self.view.__original_l1__, self.view.__original_l2__, False),
                             GitHubLinesButton.get_next_lines(self.view.__original_l1__, self.view.__original_l2__, True))
        await interaction.message.edit(content=(await get_text_from_url_and_data(
            self.view.ctx, compile_url(self.view.__original_match__.groups()), self.view.__original_match__.groups()
        ))[0], view=self.view)  # send original lines back
        self.view.ctx.bot.logger.debug(f'Back to original button pressed for MID {self.view.ctx.message.id},'
                                       f' reverting to url {self.view.__original_url__}')
