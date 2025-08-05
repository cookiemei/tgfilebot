import logging
import random
import string
import sqlite3
import sys
import asyncio
import os
import json
from dotenv import load_dotenv

from telegram import Update, BotCommand, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    JobQueue
)
from telegram.constants import ParseMode
from telegram.error import Conflict, BadRequest, TimedOut

# --- 配置和日志部分 ---
load_dotenv()
TOKEN = os.getenv("TOKEN")
CHANNEL_ID_STR = os.getenv("CHANNEL_ID")
DB_NAME = "file_storage.db"
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)
if not TOKEN or not CHANNEL_ID_STR:
    logger.error("错误：请确保 .env 文件中已正确设置 TOKEN 和 CHANNEL_ID")
    sys.exit(1)
try:
    CHANNEL_ID = int(CHANNEL_ID_STR)
except (ValueError, TypeError):
    logger.error("错误：.env 文件中的 CHANNEL_ID 必须是一个有效的整数。")
    sys.exit(1)

# --- 数据库和工具函数 ---
def init_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            file_type TEXT NOT NULL,
            file_id TEXT NOT NULL,
            key TEXT UNIQUE NOT NULL,
            original_name TEXT NOT NULL,
            custom_note TEXT NOT NULL,
            channel_msg_id INTEGER
        )
        """)
        conn.commit()
        logger.info("数据库初始化完成")
        return True
    except sqlite3.Error as e:
        logger.error(f"数据库初始化失败: {e}")
        return False
    finally:
        if conn:
            conn.close()

def generate_key():
    charset = string.ascii_letters + string.digits
    return ''.join(random.choices(charset, k=8))

# --- 异步的启动任务 ---
async def check_channel_connection(application: Application):
    try:
        await application.bot.get_chat(CHANNEL_ID)
        logger.info("频道连接测试成功")
    except Exception as e:
        logger.error(f"⚠️ 频道连接失败: {e}")
        logger.error("请确认机器人TOKEN有效，且机器人已作为管理员添加到频道中。程序即将中止。")
        raise

async def set_bot_commands(application: Application):
    commands = [
        BotCommand("start", "查看帮助"),
        BotCommand("list", "查看您的文件列表"),
        BotCommand("update", "修改文件备注"),
        BotCommand("delete", "删除一个文件"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("机器人命令设置成功")
    except TimedOut:
        logger.warning("设置机器人命令超时，已跳过。可能是网络问题。")
    except Exception as e:
        logger.error(f"设置机器人命令失败: {e}")

async def post_init(application: Application):
    await check_channel_connection(application)
    await set_bot_commands(application)

# --- 文件批处理核心功能 ---
async def process_file_batch(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.user_id
    chat_id = job.chat_id

    messages = context.chat_data.pop(f"file_batch_{user_id}", [])
    if not messages:
        logger.warning(f"process_file_batch job ran for user {user_id} but no messages were found.")
        return

    is_media_group = any(msg.media_group_id for msg in messages)
    if len(messages) == 1 and not is_media_group:
        await _handle_single_file(messages[0], context)
        return

    key = generate_key()
    file_info_list = []
    group_note = f"文件合集 (共 {len(messages)} 个)"
    caption_found = False

    for message in messages:
        if message.caption and not caption_found:
            group_note = message.caption
            caption_found = True
        file_info = _extract_file_info(message)
        if file_info:
            file_info_list.append(file_info)

    if not file_info_list:
        logger.warning(f"File batch for user {user_id} had no processable files.")
        return

    file_id_json = json.dumps(file_info_list)
    db_file_id = _save_batch_to_db(user_id, file_id_json, key, group_note)
    if not db_file_id:
        await context.bot.send_message(chat_id=chat_id, text="❌ 文件合集保存失败，请重试。")
        return
    
    try:
        await _send_batch_to_channel(db_file_id, key, group_note, file_info_list, context)
    except Exception as e:
        logger.error(f"Channel send failed for file batch {key}: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ 文件合集存储成功但频道通知失败\n\n🔑 密钥: <code>{key}</code>\n📝 备注: {group_note}", parse_mode=ParseMode.HTML)
        return

    await context.bot.send_message(chat_id=chat_id, text=f"✅ 文件合集已处理!\n\n🔑 密钥: <code>{key}</code>\n📝 备注: {group_note}\n\n您可以使用 /list 查看您的文件或 /update 修改备注", parse_mode=ParseMode.HTML)

# --- 辅助函数 ---
def _extract_file_info(message):
    if message.photo: return {'type': 'photo', 'id': message.photo[-1].file_id}
    if message.video: return {'type': 'video', 'id': message.video.file_id}
    if message.document: return {'type': 'document', 'id': message.document.file_id}
    return None

def _save_batch_to_db(user_id, file_id_json, key, note):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO files (user_id, file_type, file_id, key, original_name, custom_note) VALUES (?, ?, ?, ?, ?, ?)",
                       (user_id, 'batch', file_id_json, key, "文件合集", note))
        db_file_id = cursor.lastrowid
        conn.commit()
        logger.info(f"User {user_id} saved a file batch with key: {key}")
        return db_file_id
    except sqlite3.Error as e:
        logger.error(f"Database error for file batch {key}: {e}")
        return None
    finally:
        if conn: conn.close()

async def _send_batch_to_channel(db_file_id, key, note, file_info_list, context):
    media_group_items = [info for info in file_info_list if info['type'] in ['photo', 'video']]
    other_files = [info for info in file_info_list if info['type'] not in ['photo', 'video']]
    
    caption_sent = False
    caption_text_func = lambda: f"🔑 Key: <code>{key}</code>\n📝 Note: {note}" if not caption_sent else f"🔑 Key: <code>{key}</code>"

    if media_group_items:
        media_list = []
        for item in media_group_items:
            current_caption = f"🔑 Key: <code>{key}</code>\n📝 Note: {note}" if not caption_sent else ""
            parse_mode = ParseMode.HTML if not caption_sent else None
            if item['type'] == 'photo': media_list.append(InputMediaPhoto(media=item['id'], caption=current_caption, parse_mode=parse_mode))
            elif item['type'] == 'video': media_list.append(InputMediaVideo(media=item['id'], caption=current_caption, parse_mode=parse_mode))
            caption_sent = True
        
        channel_messages = await context.bot.send_media_group(chat_id=CHANNEL_ID, media=media_list)
        _update_channel_msg_id(db_file_id, channel_messages[0].message_id)

    for item in other_files:
        msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=item['id'], caption=caption_text_func(), parse_mode=ParseMode.HTML)
        if not caption_sent:
            _update_channel_msg_id(db_file_id, msg.message_id)
        caption_sent = True

def _update_channel_msg_id(db_file_id, channel_msg_id):
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("UPDATE files SET channel_msg_id = ? WHERE id = ?", (channel_msg_id, db_file_id))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to update channel_msg_id for db_id {db_file_id}: {e}")
    finally:
        if conn: conn.close()

async def _handle_single_file(message, context):
    user_id = message.from_user.id
    file_info = _extract_file_info(message)
    if not file_info: return

    file_type = file_info['type']
    file_id = file_info['id']
    original_name = getattr(getattr(message, file_type, None), 'file_name', f"{file_type}_{user_id}")
    
    key = generate_key()
    note = message.caption or original_name
    
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO files (user_id, file_type, file_id, key, original_name, custom_note) VALUES (?, ?, ?, ?, ?, ?)",
                       (user_id, file_type, file_id, key, original_name, note))
        db_file_id = cursor.lastrowid
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error for single file {key}: {e}"); await message.reply_text("❌ 文件保存失败，请重试"); return
    finally:
        if conn: conn.close()
        
    try:
        caption = f"🔑 Key: <code>{key}</code>\n📝 Note: {note}"
        if file_type == "video": channel_msg = await context.bot.send_video(chat_id=CHANNEL_ID, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
        elif file_type == "document": channel_msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=file_id, caption=caption, parse_mode=ParseMode.HTML)
        else: channel_msg = await context.bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=caption, parse_mode=ParseMode.HTML)
        _update_channel_msg_id(db_file_id, channel_msg.message_id)
    except Exception as e:
        logger.error(f"Channel send failed for single file {key}: {e}"); await message.reply_text(f"⚠️ 文件存储成功但频道通知失败\n\n🔑 密钥: <code>{key}</code>\n📝 备注: {note}", parse_mode=ParseMode.HTML); return
        
    await message.reply_text(f"✅ 文件已存储!\n\n🔑 密钥: <code>{key}</code>\n📝 备注: {note}\n\n您可以使用 /list 查看您的文件或 /update 修改备注", parse_mode=ParseMode.HTML)


# --- 消息和命令处理器 ---
async def handle_any_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    job_name = f"process_batch_{user_id}"
    if f"file_batch_{user_id}" not in context.chat_data:
        context.chat_data[f"file_batch_{user_id}"] = []
    context.chat_data[f"file_batch_{user_id}"].append(update.message)
    existing_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in existing_jobs:
        job.schedule_removal()
    context.job_queue.run_once(
        process_file_batch, 5, data={}, chat_id=update.effective_chat.id, user_id=user_id, name=job_name
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_html(
        f"👋 您好 {user.mention_html()}!\n\n"
        "📁 我是文件存储机器人，我的功能:\n\n"
        "1. 发送图片/视频/文档给我，我会存储它们并生成一个密钥🔑\n"
        "   (如果一次发送多张图片/视频，会共用一个密钥)\n"
        "2. 发送密钥给我，我会返回对应的文件\n"
        "3. 使用 /list 查看您的文件列表\n"
        "4. 使用 /update [密钥] [新备注] 修改文件备注\n"
        "5. 使用 /delete [密钥] 删除一个文件\n\n"
        "例如：/update ABC12345 项目最终版本"
    )

async def handle_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = update.message.text.strip()
    if len(key) != 8 or not all(c in (string.ascii_letters + string.digits) for c in key):
        await update.message.reply_text("⚠️ 密钥格式错误！请输入8位字母数字组合")
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT file_type, file_id, custom_note FROM files WHERE key = ?", (key,))
    result = cursor.fetchone()
    conn.close()

    if not result:
        await update.message.reply_text("🔍 未找到匹配文件，请检查密钥是否正确")
        return

    file_type, file_id_data, note = result
    caption = f"🔑 Key: <code>{key}</code>\n📝 Note: {note}"

    try:
        if file_type == 'batch':
            file_info_list = json.loads(file_id_data)
            media_group_to_send = [info for info in file_info_list if info['type'] in ['photo', 'video']]
            other_files_to_send = [info for info in file_info_list if info['type'] not in ['photo', 'video']]
            
            caption_sent = False
            if media_group_to_send:
                media_list = []
                for item in media_group_to_send:
                    item_caption = caption if not caption_sent else ""
                    parse_mode = ParseMode.HTML if not caption_sent else None
                    if item['type'] == 'photo': media_list.append(InputMediaPhoto(media=item['id'], caption=item_caption, parse_mode=parse_mode))
                    elif item['type'] == 'video': media_list.append(InputMediaVideo(media=item['id'], caption=item_caption, parse_mode=parse_mode))
                    caption_sent = True
                await context.bot.send_media_group(chat_id=update.effective_chat.id, media=media_list)
            
            for item in other_files_to_send:
                item_caption = caption if not caption_sent else ""
                await context.bot.send_document(chat_id=update.effective_chat.id, document=item['id'], caption=item_caption, parse_mode=ParseMode.HTML)
                caption_sent = True
        else:
            if file_type == "video": await context.bot.send_video(chat_id=update.message.chat_id, video=file_id_data, caption=caption, parse_mode=ParseMode.HTML)
            elif file_type == "document": await context.bot.send_document(chat_id=update.message.chat_id, document=file_id_data, caption=caption, parse_mode=ParseMode.HTML)
            else: await context.bot.send_photo(chat_id=update.message.chat_id, photo=file_id_data, caption=caption, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"发送文件失败 (key: {key}): {e}")
        await update.message.reply_text("❌ 文件获取失败，可能文件已被Telegram后台清理。")

async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT key, custom_note FROM files WHERE user_id = ? ORDER BY id DESC", (user_id,))
        files = cursor.fetchall()
        if not files:
            await update.message.reply_text("📭 您还没有存储任何文件")
            return
        response = "📁 您的文件列表：\n\n"
        for idx, (key, note) in enumerate(files, 1):
            response += f"{idx}. 🔑 <code>{key}</code> - 📝 {note}\n"
        response += "\n发送密钥可获取文件\n使用 /update [密钥] [新备注] 修改备注"
        await update.message.reply_text(response, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"数据库查询失败: {e}")
        await update.message.reply_text("❌ 无法获取文件列表，请重试")
    finally:
        if conn: conn.close()

async def update_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("⚠️ 格式错误！请使用：/update [密钥] [新备注]")
        return
    key = args[0]
    new_note = ' '.join(args[1:])
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT id, custom_note, channel_msg_id, file_type FROM files WHERE key = ? AND user_id = ?", (key, user_id))
        file_data = cursor.fetchone()
        if not file_data:
            await update.message.reply_text("⚠️ 更新失败！文件不存在或您不是该文件的所有者")
            return
        db_file_id, old_note, channel_msg_id, file_type = file_data
        cursor.execute("UPDATE files SET custom_note = ? WHERE id = ?", (new_note, db_file_id))
        conn.commit()
        if channel_msg_id:
            try:
                # 只有当只有一个文件时，尝试编辑标题才最有意义
                if file_type != 'batch':
                    await context.bot.edit_message_caption(chat_id=CHANNEL_ID, message_id=channel_msg_id, caption=f"🔑 Key: <code>{key}</code>\n📝 Note: {new_note}", parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"频道备注更新失败: {e}")
        await update.message.reply_text(f"✅ 备注已更新为: {new_note}")
    except Exception as e:
        logger.error(f"数据库更新失败: {e}")
        await update.message.reply_text("❌ 备注更新失败，请重试")
    finally:
        if conn: conn.close()

async def delete_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    args = context.args
    if len(args) != 1:
        await update.message.reply_text("⚠️ 格式错误！请使用：/delete [密钥]")
        return
    key = args[0]
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT channel_msg_id FROM files WHERE key = ? AND user_id = ?", (key, user_id))
        result = cursor.fetchone()
        if not result:
            await update.message.reply_text("⚠️ 删除失败！密钥不存在或您不是该文件的所有者。")
            return
        channel_msg_id = result[0]
        cursor.execute("DELETE FROM files WHERE key = ? AND user_id = ?", (key, user_id))
        conn.commit()
        if channel_msg_id:
            try:
                await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=channel_msg_id)
            except Exception as e:
                logger.warning(f"无法删除频道消息 {channel_msg_id} (可能已被删除或这是一个批处理): {e}")
        await update.message.reply_text(f"✅ 密钥 <code>{key}</code> 及其关联文件已成功删除。", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"删除操作失败: {e}")
        await update.message.reply_text("❌ 删除失败，请稍后重试。")
    finally:
        if conn: conn.close()

# --- 程序主入口 ---
def main():
    """程序主入口函数 (同步)"""
    logger.info("机器人正在启动...")
    if not init_db(): return

    application = (
        Application.builder()
        .token(TOKEN)
        .job_queue(JobQueue())
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_files))
    application.add_handler(CommandHandler("update", update_note))
    application.add_handler(CommandHandler("delete", delete_key))
    application.add_handler(MessageHandler(filters.ALL & (filters.VIDEO | filters.Document.ALL | filters.PHOTO), handle_any_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key))

    logger.info("机器人开始轮询...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
