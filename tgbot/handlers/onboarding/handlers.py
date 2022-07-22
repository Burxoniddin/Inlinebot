import datetime
from uuid import uuid4

from django.utils import timezone
from matplotlib.image import thumbnail
from telegram import ParseMode, Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import CallbackContext
from telegram.utils.helpers import escape_markdown

from tgbot.handlers.onboarding import static_text
from tgbot.handlers.utils.info import extract_user_data_from_update
from tgbot.models import User
from tgbot.handlers.onboarding.keyboards import make_keyboard_for_start_command


def command_start(update: Update, context: CallbackContext) -> None:
    u, created = User.get_user_and_created(update, context)

    if created:
        text = static_text.start_created.format(first_name=u.first_name)
    else:
        text = static_text.start_not_created.format(first_name=u.first_name)

    update.message.reply_text(text=text,
                              reply_markup=make_keyboard_for_start_command())

def secret_level(update: Update, context: CallbackContext) -> None:
    # callback_data: SECRET_LEVEL_BUTTON variable from manage_data.py
    """ Pressed 'secret_level_button_text' after /start command"""
    user_id = extract_user_data_from_update(update)['user_id']
    text = static_text.unlock_secret_room.format(
        user_count=User.objects.count(),
        active_24=User.objects.filter(updated_at__gte=timezone.now() - datetime.timedelta(hours=24)).count()
    )

    context.bot.edit_message_text(
        text=text,
        chat_id=user_id,
        message_id=update.callback_query.message.message_id,
        parse_mode=ParseMode.HTML
    )
    
from tgbot.models import Post
def inlinequery(update: Update, context: CallbackContext) -> None:
    """Handle the inline query."""
    query = update.inline_query.query

    posts = Post.objects.filter(title__icontains=query)

    results = [
        
    ]
    
    for post in posts:
        # print(f"\n\n{post.thumbnail}\n\n")
        results.append(
            InlineQueryResultArticle(
            id=str(uuid4()),
            title=post.title,
            description = post.content,
            thumb_url=f"{post.thumbnail}",
            input_message_content=InputTextMessageContent(f"{post.title} \n {post.content} \n {post.thumbnail}")
        )
        
        )
    update.inline_query.answer(results)
    