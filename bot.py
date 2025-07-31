import logging
import random
import string
import sqlite3
import sys
import asyncio
import signal
import os
from dotenv import load_dotenv

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from telegram.constants import ParseMode
from telegram.error import Conflict, BadRequest

# Load environment variables from .env file
load_dotenv()

# Get configuration from environment variables
TOKEN = os.getenv("TOKEN")
CHANNEL_ID_STR = os.getenv("CHANNEL_ID")
DB_NAME = "file_storage.db"

# 设置日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Validate that environment variables are set
if not TOKEN or not CHANNEL_ID_STR:
    logger.error("错误：请确保 .env 文件中已正确设置 TOKEN 和 CHANNEL_ID")
    sys.exit(1)

try:
    CHANNEL_ID = int(CHANNEL_ID_STR)
except (ValueError, TypeError):
    logger.error("错误：.env 文件中的 CHANNEL_ID 必须是一个有效的整数。")
    sys.exit(1)


# 初始化数据库
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
    except sqlite3.Error as e:
        logger.error(f"数据库初始化失败: {e}")
        sys.exit(1)
    finally:
        if conn:
            conn.close()

# 生成8位密钥
def generate_key():
    charset = string.ascii_letters + string.digits
    return ''.join(random.choices(charset, k=8))

# NEW FUNCTION: 设置机器人命令
async def set_bot_commands(application: Application):
    """在机器人启动时设置命令列表"""
    commands = [
        BotCommand("start", "查看帮助"),
        BotCommand("list", "查看您的文件列表"),
        BotCommand("update", "修改文件备注"),
        BotCommand("delete", "删除一个文件"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("机器人命令设置成功")
    except Exception as e:
        logger.error(f"设置机器人命令失败: {e}")

# 处理命令：帮助信息
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_html(
        f"👋 您好 {user.mention_html()}!\n\n"
        "📁 我是文件存储机器人，我的功能:\n\n"
        "1. 发送图片/视频/文档给我，我会存储它们并生成一个密钥🔑\n"
        "2. 发送密钥给我，我会返回对应的文件\n"
        "3. 使用 /list 查看您的文件列表\n"
        "4. 使用 /update [密钥] [新备注] 修改文件备注\n"
        "5. 使用 /delete [密钥] 删除一个文件\n\n"
        "例如：/update ABC12345 项目最终版本"
    )

# 处理接收的文件
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    message = update.message

    if message.video:
        file_type, file_id, file_name = "video", message.video.file_id, message.video.file_name or f"video_{user_id}"
    elif message.document:
        file_type, file_id, file_name = "document", message.document.file_id, message.document.file_name or f"document_{user_id}"
    elif message.photo:
        file_type, file_id, file_name = "photo", message.photo[-1].file_id, f"photo_{user_id}"
    else:
        return

    key = generate_key()
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO files (user_id, file_type, file_id, key, original_name, custom_note) VALUES (?, ?, ?, ?, ?, ?)",
                       (user_id, file_type, file_id, key, file_name, file_name))
        file_id_db = cursor.lastrowid
        conn.commit()
        logger.info(f"用户 {user_id} 保存了文件: {file_name} ({key})")
    except sqlite3.Error as e:
        logger.error(f"数据库错误: {e}")
        await update.message.reply_text("❌ 文件保存失败，请重试")
        return
    finally:
        if conn: conn.close()

    try:
        if file_type == "video":
            channel_msg = await context.bot.send_video(chat_id=CHANNEL_ID, video=file_id, caption=f"🔑 Key: <code>{key}</code>\n📝 Note: {file_name}", parse_mode=ParseMode.HTML)
        elif file_type == "document":
            channel_msg = await context.bot.send_document(chat_id=CHANNEL_ID, document=file_id, caption=f"🔑 Key: <code>{key}</code>\n📝 Note: {file_name}", parse_mode=ParseMode.HTML)
        else:
            channel_msg = await context.bot.send_photo(chat_id=CHANNEL_ID, photo=file_id, caption=f"🔑 Key: <code>{key}</code>\n📝 Note: {file_name}", parse_mode=ParseMode.HTML)

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("UPDATE files SET channel_msg_id = ? WHERE id = ?", (channel_msg.message_id, file_id_db))
        conn.commit()
    except Exception as e:
        logger.error(f"频道发送失败: {e}")
        await update.message.reply_text(f"⚠️ 文件存储成功但频道通知失败\n\n🔑 密钥: <code>{key}</code>\n📝 备注: {file_name}", parse_mode=ParseMode.HTML)
        return
    finally:
        if conn: conn.close()

    await update.message.reply_text(f"✅ 文件已存储!\n\n🔑 密钥: <code>{key}</code>\n📝 备注: {file_name}\n\n您可以使用 /list 查看您的文件或 /update [密钥] [新备注] 修改备注", parse_mode=ParseMode.HTML)

# 处理密钥请求
async def handle_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    requesting_user_id = update.message.from_user.id
    key = update.message.text.strip()

    if len(key) != 8 or not all(c in (string.ascii_letters + string.digits) for c in key):
        await update.message.reply_text("⚠️ 密钥格式错误！请输入8位字母数字组合")
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT file_type, file_id, custom_note FROM files WHERE key = ?", (key,))
        result = cursor.fetchone()

        if not result:
            await update.message.reply_text("🔍 未找到匹配文件，请检查密钥是否正确")
            return

        file_type, file_id, note = result

        caption = f"🔑 Key: <code>{key}</code>\n📝 Note: {note}"
        if file_type == "video":
            await context.bot.send_video(chat_id=update.message.chat_id, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
        elif file_type == "document":
            await context.bot.send_document(chat_id=update.message.chat_id, document=file_id, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await context.bot.send_photo(chat_id=update.message.chat_id, photo=file_id, caption=caption, parse_mode=ParseMode.HTML)
        logger.info(f"用户 {requesting_user_id} 通过密钥 {key} 获取了文件")

    except Exception as e:
        logger.error(f"发送文件失败: {e}")
        await update.message.reply_text("❌ 文件获取失败，请稍后再试")
    finally:
        if conn: conn.close()

# 查看文件列表
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
        logger.info(f"用户 {user_id} 查看了文件列表")

    except Exception as e:
        logger.error(f"数据库查询失败: {e}")
        await update.message.reply_text("❌ 无法获取文件列表，请重试")
    finally:
        if conn: conn.close()

# 更新文件备注
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
        cursor.execute("SELECT id, custom_note, channel_msg_id FROM files WHERE key = ? AND user_id = ?", (key, user_id))
        file_data = cursor.fetchone()

        if not file_data:
            await update.message.reply_text("⚠️ 更新失败！文件不存在或您不是该文件的所有者")
            return

        file_id_db, old_note, channel_msg_id = file_data
        cursor.execute("UPDATE files SET custom_note = ? WHERE key = ? AND user_id = ?", (new_note, key, user_id))
        conn.commit()

        if channel_msg_id:
            try:
                await context.bot.edit_message_caption(chat_id=CHANNEL_ID, message_id=channel_msg_id, caption=f"🔑 Key: <code>{key}</code>\n📝 Note: {new_note}", parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"频道备注更新失败: {e}")

        await update.message.reply_text(f"✅ 备注已更新为: {new_note}")
        logger.info(f"用户 {user_id} 更新了文件 {key} 的备注: {old_note} → {new_note}")

    except Exception as e:
        logger.error(f"数据库更新失败: {e}")
        await update.message.reply_text("❌ 备注更新失败，请重试")
    finally:
        if conn: conn.close()

# 删除密钥和文件
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
            except BadRequest as e:
                logger.warning(f"无法删除频道消息 {channel_msg_id} (可能已被删除): {e}")
            except Exception as e:
                logger.error(f"删除频道消息 {channel_msg_id} 失败: {e}")

        await update.message.reply_text(f"✅ 密钥 <code>{key}</code> 及其关联文件已成功删除。", parse_mode=ParseMode.HTML)
        logger.info(f"用户 {user_id} 删除了密钥 {key}")

    except Exception as e:
        logger.error(f"删除操作失败: {e}")
        await update.message.reply_text("❌ 删除失败，请稍后重试。")
    finally:
        if conn: conn.close()

# 检查频道连接
async def check_channel_connection(application: Application):
    try:
        await application.bot.get_chat(CHANNEL_ID)
        logger.info("频道连接测试成功")
    except Exception as e:
        logger.error(f"⚠️ 频道连接失败: {e}")
        logger.error("请确认机器人已作为管理员添加到频道中。")
        sys.exit(1)

# 主函数
def main():
    """机器人主入口函数"""
    logger.info("机器人正在启动...")
    init_db()

    try:
        application = Application.builder().token(TOKEN).build()
    except Exception as e:
        logger.error(f"创建Application失败: {e}")
        sys.exit(1)

    # 添加命令处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_files))
    application.add_handler(CommandHandler("update", update_note))
    application.add_handler(CommandHandler("delete", delete_key))

    # 添加消息处理器
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.ALL | filters.PHOTO, handle_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key))

    loop = asyncio.get_event_loop()
    # MODIFIED: 在启动时设置命令并检查连接
    loop.run_until_complete(set_bot_commands(application))
    loop.run_until_complete(check_channel_connection(application))

    try:
        logger.info("机器人开始轮询...")
        application.run_polling(drop_pending_updates=True)
    except Conflict as e:
        logger.error(f"机器人启动冲突: {e}。请确保没有其他实例正在运行。")
        sys.exit(1)
    except Exception as e:
        logger.error(f"机器人运行异常: {str(e)}")
    finally:
        logger.info("机器人已停止")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    main()