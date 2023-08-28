import asyncio
import types
import typing

from vkwave.api import API, APIOptionsRequestContext
from vkwave.api.token.token import (
    BotSyncPoolTokens,
    BotSyncSingleToken,
    Token,
    UserSyncSingleToken,
)
from vkwave.bots import (
    BaseEvent,
    BotEvent,
    BotLongpollExtension,
    BotType,
    ChatActionFilter,
    CommandsFilter,
    DefaultRouter,
    Dispatcher,
    EventTypeFilter,
    FlagFilter,
    FromGroupFilter,
    FromIdFilter,
    FromMeFilter,
    FwdMessagesFilter,
    GroupId,
    IsAdminFilter,
    LevenshteinFilter,
    MessageArgsFilter,
    MessageFromConversationTypeFilter,
    PayloadFilter,
    PeerIdFilter,
    RegexFilter,
    ReplyMessageFilter,
    StickerFilter,
    TextContainsFilter,
    TextFilter,
    TextStartswithFilter,
    TokenStorage,
    UserEvent,
    UserId,
    UserLongpollExtension,
    UserTokenStorage,
)
from vkwave.bots.addons.easy.easy_handlers import (
    SimpleBotCallback,
    SimpleBotEvent,
    SimpleUserEvent,
)
from vkwave.bots.core import BaseFilter
from vkwave.bots.core.dispatching.dp.middleware.middleware import BaseMiddleware, MiddlewareResult
from vkwave.bots.core.dispatching.filters.builtin import (
    AttachmentTypeFilter,
    PayloadContainsFilter,
)
from vkwave.bots.core.dispatching.filters.extension_filters import VBMLFilter
from vkwave.bots.core.dispatching.router.router import BaseRouter
from vkwave.bots.fsm.filters import StateFilter
from vkwave.client import AIOHTTPClient
from vkwave.longpoll import BotLongpoll, BotLongpollData, UserLongpoll, UserLongpollData
from vkwave.types.bot_events import BotEventType
from vkwave.types.user_events import EventId


class _APIContextManager:
    def __init__(
        self, tokens: typing.Union[str, typing.List[str]], bot_type: BotType, client: AIOHTTPClient
    ):
        self.client = client
        if bot_type.USER:
            self.tokens = (
                UserSyncSingleToken(Token(tokens))
                if isinstance(tokens, str)
                else BotSyncPoolTokens([Token(token) for token in tokens])
            )
        else:
            self.tokens = (
                BotSyncSingleToken(Token(tokens))
                if isinstance(tokens, str)
                else BotSyncPoolTokens([Token(token) for token in tokens])
            )
        self.api = API(clients=self.client, tokens=self.tokens)

    async def __aenter__(self):
        return self.api.get_context()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        await self.client.close()


def create_api_session_aiohttp(
    token: str, bot_type: BotType = BotType.BOT, client: typing.Optional[AIOHTTPClient] = None
) -> _APIContextManager:
    return _APIContextManager(token, bot_type, client or AIOHTTPClient())


class BaseSimpleLongPollBot:
    def __init__(
        self,
        tokens: typing.Union[str, typing.List[str]],
        bot_type: BotType,
        router: typing.Optional[BaseRouter] = None,
        group_id: typing.Optional[int] = None,
        client: typing.Optional[AIOHTTPClient] = None,
        uvloop: bool = False,
        event: typing.Optional[
            typing.Union[typing.Type[SimpleBotEvent], typing.Type[SimpleUserEvent]]
        ] = None,
    ):
        if uvloop:
            import uvloop

            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

        self.context = types.SimpleNamespace()
        self.group_id = group_id
        self.bot_type = bot_type
        self.client = client or AIOHTTPClient()
        self.api_session = create_api_session_aiohttp(tokens, bot_type, self.client)
        self.api_context: APIOptionsRequestContext = self.api_session.api.get_context()
        if self.bot_type is BotType.USER:
            self.SimpleBotEvent = SimpleUserEvent
            self._lp = UserLongpoll(self.api_context, UserLongpollData())
            self._token_storage = UserTokenStorage[UserId](tokens)
            self.dispatcher = Dispatcher(self.api_session.api, self._token_storage)
            self._lp = UserLongpollExtension(self.dispatcher, self._lp)
        else:
            self.SimpleBotEvent = SimpleBotEvent
            self._lp = BotLongpoll(self.api_context, BotLongpollData(group_id))
            self._token_storage = TokenStorage[GroupId]()
            self.dispatcher = Dispatcher(self.api_session.api, self._token_storage)
            self._lp = BotLongpollExtension(self.dispatcher, self._lp)

        self.event = event or self.SimpleBotEvent

        self.middleware_manager = self.dispatcher.middleware_manager  # auf
        self.add_middleware = self.middleware_manager.add_middleware

        self.router = router or DefaultRouter()
        self.dispatcher.add_router(self.router)

        self.args_filter = MessageArgsFilter
        self.attachment_type_filter = AttachmentTypeFilter
        self.chat_action_filter = ChatActionFilter
        self.command_filter = CommandsFilter
        self.conversation_type_filter = MessageFromConversationTypeFilter
        self.event_type_filter = EventTypeFilter
        self.fwd_filter = FwdMessagesFilter
        self.from_id_filter = FromIdFilter
        self.from_group_filter = FromGroupFilter
        self.flag_filter = FlagFilter
        self.is_admin_filter = IsAdminFilter
        self.levenshtein_filter = LevenshteinFilter
        self.payload_contains_filter = PayloadContainsFilter
        self.payload_filter = PayloadFilter
        self.peer_id_filter = PeerIdFilter
        self.regex_filter = RegexFilter
        self.reply_filter = ReplyMessageFilter
        self.state_filter = StateFilter
        self.sticker_filter = StickerFilter
        self.text_contains_filter = TextContainsFilter
        self.text_filter = TextFilter
        self.text_startswith_filter = TextStartswithFilter
        self.vbml_filter = VBMLFilter
        if self.bot_type is BotType.USER:
            self.from_me_filter = FromMeFilter

    class SimpleBotMiddleware(BaseMiddleware):
        async def pre_process_event(self, event: BaseEvent) -> MiddlewareResult:
            pass

    def handler(self, *filters: BaseFilter):
        """
        Handler for all events
        """

        def decorator(func: typing.Callable[..., typing.Any]):
            record = self.router.registrar.new()
            record.with_filters(*filters)
            record.handle(SimpleBotCallback(func, self.bot_type, self.event))
            self.router.registrar.register(record.ready())
            return func

        return decorator

    def message_handler(self, *filters: BaseFilter):
        """
        Handler only for message events
        """

        def decorator(func: typing.Callable[..., typing.Any]):
            record = self.router.registrar.new()
            record.with_filters(*filters)
            if self.bot_type is BotType.BOT:
                record.filters.append(EventTypeFilter(BotEventType.MESSAGE_NEW))
            else:
                record.filters.append(EventTypeFilter(EventId.MESSAGE_EVENT.value))
            record.handle(SimpleBotCallback(func, self.bot_type, self.event))
            self.router.registrar.register(record.ready())
            return func

        return decorator

    def middleware(self):
        def decorator(
            func: typing.Callable[[typing.Union[UserEvent, BotEvent]], MiddlewareResult]
        ):
            middleware = self.SimpleBotMiddleware()
            middleware.pre_process_event = func
            self.middleware_manager.add_middleware(middleware)

            return func

        return decorator

    async def run(self, ignore_errors: bool = True):
        if self.bot_type is BotType.BOT:
            await self.dispatcher.cache_potential_tokens()
        await self._lp.start(ignore_errors)

    def run_forever(
        self, ignore_errors: bool = True, loop: typing.Optional[asyncio.AbstractEventLoop] = None
    ):
        loop = loop or asyncio.get_event_loop()
        loop.create_task(self.run(ignore_errors))
        loop.run_forever()
