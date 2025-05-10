import discord
from discord.ext import commands, tasks
import datetime
import json
import os
import pytz # タイムゾーン対応
import jpholiday # 祝日判定
from dotenv import load_dotenv # .envファイル読み込み用

# --- .envファイルから環境変数を読み込む (ローカルテスト用) ---
# Koyebなどの本番環境では、プラットフォームの環境変数設定機能を使う
load_dotenv()

# --- 設定 (環境変数から取得) ---
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID_STR = os.getenv("DISCORD_GUILD_ID")
GUILD_ID = int(GUILD_ID_STR) if GUILD_ID_STR and GUILD_ID_STR.isdigit() else None # テストサーバーID (任意)

# データ保存ディレクトリ (Koyebの永続ディスクマウントポイントを想定)
# 環境変数 DATA_STORAGE_PATH が設定されていなければカレントディレクトリを使用
DATA_STORAGE_PATH = os.getenv("DATA_STORAGE_PATH", ".")
# DATA_STORAGE_PATH が存在し、かつカレントディレクトリでない場合に作成を試みる
if DATA_STORAGE_PATH != "." and not os.path.exists(DATA_STORAGE_PATH):
    try:
        os.makedirs(DATA_STORAGE_PATH, exist_ok=True)
        print(f"データ保存ディレクトリ '{DATA_STORAGE_PATH}' を作成しました。")
    except OSError as e:
        print(f"警告: データ保存ディレクトリ '{DATA_STORAGE_PATH}' の作成に失敗しました: {e}")
        print(f"フォールバックとしてカレントディレクトリにデータを保存します。")
        DATA_STORAGE_PATH = "." # 作成失敗時はカレントディレクトリにフォールバック

DATA_FILE = os.path.join(DATA_STORAGE_PATH, "duty_data.json")
JST = pytz.timezone('Asia/Tokyo')

# --- データ管理 ---
default_data = {
    "members": [],
    "current_duty_index": 0,
    "last_rotation_date": None,
    "last_bot_check_date": None,
    "extra_activity_days": [],
    "absentees": {}
}
data = default_data.copy()

def load_data():
    global data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            try:
                loaded = json.load(f)
                for key, value in default_data.items():
                    if key not in loaded:
                        loaded[key] = value
                data = loaded
                print(f"データを '{DATA_FILE}' から正常に読み込みました。")
            except json.JSONDecodeError:
                print(f"警告: '{DATA_FILE}' の読み込みに失敗しました。破損している可能性があります。デフォルトデータを使用します。")
                data = default_data.copy()
                # 破損時はバックアップを作るなどの処理も考えられる
    else:
        print(f"'{DATA_FILE}' が見つかりません。新規に作成します。")
        data = default_data.copy()
        save_data() # 初回起動時に空のファイルを作成

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        # print(f"データを '{DATA_FILE}' に保存しました。") # 頻繁に出力するとログが煩雑になるのでコメントアウトも検討
    except IOError as e:
        print(f"エラー: '{DATA_FILE}' へのデータ保存に失敗しました: {e}")


# --- Botの準備 ---
intents = discord.Intents.default()
# intents.message_content = True # プレフィックスコマンドを使わない場合は不要
bot = discord.Bot(intents=intents)

# --- ヘルパー関数 (欠席関連) ---
def is_member_absent(member_name: str, check_date: datetime.date, absentees_data: dict) -> bool:
    if member_name in absentees_data:
        absence_info = absentees_data[member_name]
        if absence_info.get("until") is None:
            return True
        try:
            until_date = datetime.datetime.strptime(absence_info["until"], "%Y-%m-%d").date()
            return check_date <= until_date
        except (ValueError, TypeError):
            print(f"警告: メンバー '{member_name}' の欠席終了日フォーマットが不正です: {absence_info.get('until')}")
            return False # 安全のため欠席ではないと扱う
    return False

def check_and_remove_expired_absentees(today_date_obj: datetime.date):
    absentees_to_remove = []
    for member_name, absence_info in data["absentees"].items():
        if absence_info.get("until"):
            try:
                until_date = datetime.datetime.strptime(absence_info["until"], "%Y-%m-%d").date()
                if today_date_obj > until_date:
                    absentees_to_remove.append(member_name)
            except (ValueError, TypeError):
                print(f"警告: メンバー '{member_name}' の欠席終了日フォーマットが不正 (期限切れチェック時): {absence_info.get('until')}")
                continue
    updated = False
    for member_name in absentees_to_remove:
        if member_name in data["absentees"]: # 万が一のキーエラーを防ぐ
            del data["absentees"][member_name]
            print(f"情報: メンバー '{member_name}' の欠席期間が終了したため、自動復帰させました。")
            updated = True
    if updated:
        save_data()

def find_next_active_member_index(start_search_idx: int, members_list: list, check_date: datetime.date, absentees_data: dict) -> int | None:
    if not members_list:
        return None
    num_members = len(members_list)
    for i in range(num_members):
        current_check_idx = (start_search_idx + i) % num_members
        member_name = members_list[current_check_idx]
        if not is_member_absent(member_name, check_date, absentees_data):
            return current_check_idx
    return None

# --- ヘルパー関数 (既存の修正・追加) ---
def get_actual_duty_person_for_date(target_date: datetime.date, nominal_duty_idx: int, current_data: dict) -> str | None:
    # この関数内では load_data() や check_and_remove_expired_absentees() を直接呼ばない
    # 呼び出し元でデータの一貫性を保つ
    if not current_data["members"]:
        return None
    actual_duty_idx = find_next_active_member_index(nominal_duty_idx, current_data["members"], target_date, current_data["absentees"])
    if actual_duty_idx is not None:
        return current_data["members"][actual_duty_idx]
    return None

def get_current_actual_duty_person_name(current_data: dict) -> str:
    today = datetime.datetime.now(JST).date()
    # この関数呼び出し前に check_and_remove_expired_absentees が実行されている想定
    person = get_actual_duty_person_for_date(today, current_data["current_duty_index"], current_data)
    if person:
        return person
    elif not current_data["members"]:
        return "(メンバー未登録)"
    else:
        return "(現在、割り当て可能な当番がいません)"

def is_holiday(date_obj: datetime.date) -> bool:
    return jpholiday.is_holiday(date_obj)

def is_activity_day(date_obj: datetime.date, extra_activity_days: list) -> bool:
    date_str = date_obj.isoformat()
    if date_str in extra_activity_days:
        return True
    if is_holiday(date_obj):
        return False
    weekday = date_obj.weekday()
    return weekday in [0, 2, 4] # 月・水・金

# --- イベント ---
@bot.event
async def on_ready():
    print(f'{bot.user.name} としてログインしました')
    load_data() # Bot起動時にデータをロード
    # GUILD_ID が設定されていれば、そのギルドにコマンドを即時登録
    guild_ids_list = [GUILD_ID] if GUILD_ID else None # slash_commandデコレータに渡すため
    
    # スラッシュコマンドの同期 (on_ready内で行うのが一般的)
    if guild_ids_list:
        for guild_id_val in guild_ids_list: # GUILD_IDがリストになる可能性を考慮
            try:
                guild_obj = discord.Object(id=guild_id_val)
                await bot.tree.sync(guild=guild_obj)
                print(f"コマンドをギルド {guild_id_val} に同期しました。")
            except Exception as e:
                print(f"ギルド {guild_id_val} へのコマンド同期に失敗: {e}")
    else:
        try:
            await bot.tree.sync() # グローバルコマンドとして同期
            print("コマンドをグローバルに同期しました。(反映に時間がかかる場合があります)")
        except Exception as e:
            print(f"グローバルコマンドの同期に失敗: {e}")

    daily_update_task.start() # 定期タスクを開始


# --- 定期タスク (日替わり処理) ---
@tasks.loop(time=datetime.time(hour=0, minute=1, tzinfo=JST)) # 日本時間の0時1分に実行
async def daily_update_task():
    # print("DEBUG: daily_update_task が呼び出されました。") # デバッグ用
    load_data() # 各実行前に最新データを読み込む
    today_date_obj = datetime.datetime.now(JST).date()
    today_str = today_date_obj.isoformat()

    check_and_remove_expired_absentees(today_date_obj) # 最初に期限切れ欠席者を処理

    if data.get("last_bot_check_date") == today_str:
        # print(f"DEBUG: {today_str} の日付処理は既に実行済みです。")
        return

    print(f"日付処理タスク実行: {today_str}")

    if is_activity_day(today_date_obj, data["extra_activity_days"]):
        print(f"{today_str} は活動日です。")
        if data.get("last_rotation_date") != today_str:
            if data["members"]:
                num_members = len(data["members"])
                # 次の当番の「名目上の」インデックス
                # 前回ローテーション時の当番から見て次の人 (欠席考慮前)
                # ただし、誰も当番がいなかった場合やメンバー変更があった場合を考慮し、
                # current_duty_indexをベースに+1する方がシンプル
                next_nominal_duty_index_start_search = (data["current_duty_index"] + 1) % num_members
                
                actual_new_duty_index = find_next_active_member_index(
                    next_nominal_duty_index_start_search,
                    data["members"],
                    today_date_obj,
                    data["absentees"]
                )

                if actual_new_duty_index is not None:
                    # 更新前の実際の当番を取得 (ログ用)
                    old_actual_duty_person = get_actual_duty_person_for_date(today_date_obj, data["current_duty_index"], data)
                    
                    data["current_duty_index"] = actual_new_duty_index # 名目上のインデックスを更新
                    data["last_rotation_date"] = today_str
                    new_actual_duty_person = data["members"][actual_new_duty_index]
                    print(f"当番を更新しました: {old_actual_duty_person or '(前当番不明)'} -> {new_actual_duty_person}")
                    
                    # Discordに通知 (通知チャンネルIDを環境変数で設定)
                    notification_channel_id_str = os.getenv("NOTIFICATION_CHANNEL_ID")
                    if notification_channel_id_str and notification_channel_id_str.isdigit():
                        channel_id = int(notification_channel_id_str)
                        channel = bot.get_channel(channel_id)
                        if channel:
                            try:
                                await channel.send(f"【鍵当番】本日の鍵当番は **{new_actual_duty_person}** さんです。活動日です！")
                            except discord.Forbidden:
                                print(f"警告: チャンネル {channel_id} へのメッセージ送信権限がありません。")
                            except Exception as e:
                                print(f"警告: 通知メッセージ送信中にエラー: {e}")
                        else:
                            print(f"警告: 通知チャンネルID {channel_id} が見つかりません。")
                else:
                    print(f"{today_str}は活動日ですが、割り当て可能なアクティブな当番がいません。ローテーションは行われませんでした。")
            else:
                print("当番メンバーがいません。ローテーションは行われませんでした。")
        else:
            print(f"{today_str} の当番ローテーションは既に実行済みか、不要です。")
    else:
        print(f"{today_str} は活動日ではありません。当番のローテーションは行いません。")

    data["last_bot_check_date"] = today_str
    save_data() # 全ての処理の最後にデータを保存

@daily_update_task.before_loop
async def before_daily_update_task():
    await bot.wait_until_ready() # Botが完全に準備できるまで待つ
    print("日替わり当番更新タスクの準備完了。指定時刻にループ開始。")
    # Bot起動時に、まだ今日の処理が終わっていなければ一度実行する
    load_data()
    today_date_obj = datetime.datetime.now(JST).date()
    check_and_remove_expired_absentees(today_date_obj)

    if data.get("last_bot_check_date") != today_date_obj.isoformat():
        print("起動時チェック: 今日の日付処理を試みます...")
        # daily_update_task とほぼ同じロジックを実行（重複を避けるため主要部分のみ）
        if is_activity_day(today_date_obj, data["extra_activity_days"]):
            if data.get("last_rotation_date") != today_date_obj.isoformat():
                if data["members"]:
                    num_members = len(data["members"])
                    next_nominal_idx = (data["current_duty_index"] + 1) % num_members
                    actual_new_idx = find_next_active_member_index(
                        next_nominal_idx, data["members"], today_date_obj, data["absentees"]
                    )
                    if actual_new_idx is not None:
                        data["current_duty_index"] = actual_new_idx
                        data["last_rotation_date"] = today_date_obj.isoformat()
                        print(f"起動時: {today_date_obj.isoformat()} が活動日のため当番を更新しました。新しい当番: {data['members'][actual_new_idx]}")
        data["last_bot_check_date"] = today_date_obj.isoformat()
        save_data()
        print("起動時の日付処理チェック完了。")
    else:
        print("起動時チェック: 今日の日付処理は既に完了しているようです。")


# --- スラッシュコマンド ---
guild_ids_list = [GUILD_ID] if GUILD_ID else None

@bot.slash_command(name="今日の当番", description="今日の鍵当番と活動日かを表示します。", guild_ids=guild_ids_list)
async def today_duty(ctx: discord.ApplicationContext):
    load_data() # 常に最新のデータを参照
    today_date_obj = datetime.datetime.now(JST).date()
    check_and_remove_expired_absentees(today_date_obj) # 表示前に欠席情報を更新

    # Botがオフラインだった場合などに日付処理が遅れている可能性を考慮し、ユーザーに通知
    is_today_checked = data.get("last_bot_check_date") == today_date_obj.isoformat()
    is_today_activity_day = is_activity_day(today_date_obj, data["extra_activity_days"])
    is_rotation_done_for_today = data.get("last_rotation_date") == today_date_obj.isoformat()

    if is_today_activity_day and not is_rotation_done_for_today and not is_today_checked :
        await ctx.response.defer(ephemeral=True)
        await ctx.followup.send("現在、日付更新処理が進行中の可能性があります。数秒後にもう一度お試しください。", ephemeral=True)
        # daily_update_task.restart() # 強制実行は競合の可能性があるので慎重に
        return

    duty_person_name = get_current_actual_duty_person_name(data)
    activity_status = "活動日です。" if is_today_activity_day else "活動日ではありません。"
    
    nominal_duty_person_today = data["members"][data["current_duty_index"]] if data["members"] and 0 <= data["current_duty_index"] < len(data["members"]) else None
    absent_note = ""
    if nominal_duty_person_today and is_member_absent(nominal_duty_person_today, today_date_obj, data["absentees"]) and nominal_duty_person_today != duty_person_name :
        absent_note = f" (本来の当番 {nominal_duty_person_today} さんは欠席中のため、{duty_person_name} さんが担当)"
    elif duty_person_name == "(現在、割り当て可能な当番がいません)":
        absent_note = " (全員欠席またはアクティブなメンバーがいません)"

    await ctx.respond(f"今日の鍵当番は **{duty_person_name}** さんです。\n今日は {activity_status}{absent_note}")


@bot.slash_command(name="明日の当番", description="明日の鍵当番（または次の活動日の当番）を表示します。", guild_ids=guild_ids_list)
async def tomorrow_duty(ctx: discord.ApplicationContext):
    load_data()
    today_date_obj = datetime.datetime.now(JST).date()
    # 明日以降の欠席状況も考慮するため、チェックする日付は探索しながら決める
    
    if not data["members"]:
        await ctx.respond("当番メンバーが登録されていません。", ephemeral=True)
        return

    current_nominal_idx = data["current_duty_index"]
    next_activity_date_found = None
    next_duty_person_found = None

    # 今日の次のローテーションからアクティブな人を探す、という考え方で次の活動日を見つける
    # 1. まず次の活動日を見つける
    # 2. その活動日での当番を計算する (今日の当番から何ローテーション後か)

    temp_nominal_idx_for_search = current_nominal_idx # 今日の名目上の当番
    rotations_count = 0 # 今日から何回ローテーションしたか

    for i in range(1, 366): # 今日から最大1年先まで探索
        check_date = today_date_obj + datetime.timedelta(days=i)
        check_and_remove_expired_absentees(check_date) # その日の欠席状況を最新にする

        if is_activity_day(check_date, data["extra_activity_days"]):
            rotations_count += 1 # 1活動日進んだ
            
            # この活動日での当番候補を探す (今日の当番から rotations_count 回ローテーションした後の名目上のインデックスから探索)
            prospective_nominal_idx_for_this_day = (current_nominal_idx + rotations_count) % len(data["members"])

            actual_person_idx = find_next_active_member_index(
                prospective_nominal_idx_for_this_day,
                data["members"],
                check_date, # この日にアクティブか
                data["absentees"]
            )
            if actual_person_idx is not None:
                next_activity_date_found = check_date
                next_duty_person_found = data["members"][actual_person_idx]
                break 
            # else: この活動日ではアクティブな人がいなかった。次の活動日を探す。
    
    if next_activity_date_found and next_duty_person_found:
        day_str = "明日" if (next_activity_date_found - today_date_obj).days == 1 else f"{next_activity_date_found.strftime('%Y年%m月%d日(%a)')}"
        await ctx.respond(f"{day_str}は活動日です。その時の鍵当番は **{next_duty_person_found}** さんです。")
    else:
        await ctx.respond("今後1年以内に次の活動予定日または割り当て可能な当番が見つかりませんでした。")


@bot.slash_command(name="当番表", description="現在の当番表とローテーション（欠席者考慮）を表示します。", guild_ids=guild_ids_list)
async def duty_list_command(ctx: discord.ApplicationContext):
    load_data()
    today = datetime.datetime.now(JST).date()
    check_and_remove_expired_absentees(today)

    if not data["members"]:
        await ctx.respond("当番メンバーはまだ登録されていません。", ephemeral=True)
        return

    message = "現在の当番表 (ローテーション順):\n"
    actual_today_duty_person = get_current_actual_duty_person_name(data)

    for i, member_name in enumerate(data["members"]):
        is_absent_today = is_member_absent(member_name, today, data["absentees"])
        absent_marker = " (欠席中)" if is_absent_today else ""
        
        is_nominal_today = (i == data["current_duty_index"])

        if member_name == actual_today_duty_person and not is_absent_today :
            message += f"➡️ **{member_name}** (今日の当番){absent_marker}\n"
        elif is_nominal_today and is_absent_today:
            # 名目上の当番だが欠席中の場合は、代わりに担当する人がいればそちらに矢印がつくはず
            message += f"   *{member_name}* (名目上の当番 - 欠席中)\n"
        else:
            message += f"   {member_name}{absent_marker}\n"
            
    activity_status = "活動日です。" if is_activity_day(today, data["extra_activity_days"]) else "活動日ではありません。"
    message += f"\n今日は {today.strftime('%Y年%m月%d日(%a)')}、{activity_status}"
    if data.get("last_rotation_date"):
        message += f"\n最終当番更新日 (活動日): {data['last_rotation_date']}"
    if actual_today_duty_person == "(現在、割り当て可能な当番がいません)":
         message += "\n<注意> 現在、活動可能な当番がいません。"
    await ctx.respond(message)

# --- 管理者向けコマンドグループ (当番管理) ---
admin_group = bot.create_group(
    name="当番管理",
    description="鍵当番のメンバーを管理します（管理者のみ）",
    guild_ids=guild_ids_list # GUILD_ID があればそれを指定
)

@admin_group.command(name="登録", description="当番メンバーを登録。スペース区切りで複数名。")
@commands.has_permissions(administrator=True)
async def add_members(ctx: discord.ApplicationContext, members_input: discord.Option(str, description="登録するメンバー名 (スペース区切り)", required=True)):
    load_data()
    new_members_list = members_input.split()
    if not new_members_list:
        await ctx.respond("登録するメンバー名を入力してください。", ephemeral=True)
        return
    added_names = []
    already_exists_names = []
    for member_name in new_members_list:
        if member_name not in data["members"]:
            data["members"].append(member_name)
            added_names.append(member_name)
        else:
            already_exists_names.append(member_name)
    
    response_message = ""
    if added_names:
        save_data() # 変更があった場合のみ保存
        response_message += f"メンバー「{', '.join(added_names)}」を当番リストに追加しました。\n"
    if already_exists_names:
        response_message += f"メンバー「{', '.join(already_exists_names)}」は既に登録されています。\n"
    
    if not response_message: # 何も処理されなかった場合
        response_message = "指定されたメンバーは全員登録済みか、入力がありませんでした。"
    
    await ctx.respond(response_message, ephemeral=True)
    if added_names: # 追加があった場合、更新後のリストをフォローアップで表示
        await duty_list_command(ctx)


@admin_group.command(name="削除", description="当番メンバーを削除します。")
@commands.has_permissions(administrator=True)
async def remove_member(ctx: discord.ApplicationContext, member_name: discord.Option(str, description="削除するメンバー名", required=True)):
    load_data()
    if member_name in data["members"]:
        if member_name in data["absentees"]:
            del data["absentees"][member_name]
            
        removed_member_index = data["members"].index(member_name)
        data["members"].remove(member_name)

        if not data["members"]:
            data["current_duty_index"] = 0
        else:
            # 削除されたメンバーが現在の名目上の当番より前か、同じか、後かでインデックスを調整
            if removed_member_index < data["current_duty_index"]:
                data["current_duty_index"] -= 1
            # 削除されたのが名目上の当番で、かつリストの最後にいた場合などは0に戻す必要があるので、
            # インデックスがリストの範囲を超えないように調整
            if data["current_duty_index"] >= len(data["members"]):
                data["current_duty_index"] = 0 if data["members"] else 0 # メンバーがいれば0、いなければそのまま0
            # 最終的なインデックスの範囲チェック
            data["current_duty_index"] = max(0, min(data["current_duty_index"], len(data["members"]) -1 if data["members"] else 0))

        save_data()
        await ctx.respond(f"メンバー「{member_name}」を当番リストおよび欠席リストから削除しました。", ephemeral=True)
        if data["members"]:
            await duty_list_command(ctx)
        else:
            await ctx.followup.send("当番メンバーがいなくなりました。", ephemeral=True)
    else:
        await ctx.respond(f"メンバー「{member_name}」は見つかりませんでした。", ephemeral=True)

@admin_group.command(name="クリア", description="当番リストと欠席者リストを全てクリアします。")
@commands.has_permissions(administrator=True)
async def clear_members(ctx: discord.ApplicationContext):
    load_data()
    data["members"] = []
    data["current_duty_index"] = 0
    data["absentees"] = {}
    # last_rotation_date などもリセットするかは仕様によるが、ここではメンバーリスト関連のみ
    save_data()
    await ctx.respond("当番リストと欠席者リストをクリアしました。", ephemeral=True)


@admin_group.command(name="手動更新", description="当番を次のアクティブな人に手動で更新します。")
@commands.has_permissions(administrator=True)
async def manual_update(ctx: discord.ApplicationContext):
    load_data()
    today = datetime.datetime.now(JST).date()
    check_and_remove_expired_absentees(today)

    if not data["members"]:
        await ctx.respond("当番メンバーが登録されていません。", ephemeral=True)
        return

    old_actual_duty_person = get_actual_duty_person_for_date(today, data["current_duty_index"], data)
    
    num_members = len(data["members"])
    # 次の当番の「名目上の」インデックスを開始点として探索
    next_nominal_idx_start_search = (data["current_duty_index"] + 1) % num_members
    
    actual_new_duty_idx = find_next_active_member_index(
        next_nominal_idx_start_search, data["members"], today, data["absentees"]
    )

    if actual_new_duty_idx is not None:
        data["current_duty_index"] = actual_new_duty_idx
        data["last_rotation_date"] = today.isoformat() # 手動更新日を記録
        save_data()
        new_actual_duty_person = data["members"][actual_new_duty_idx]
        await ctx.respond(f"当番を手動で更新しました。\n旧当番: {old_actual_duty_person or '(該当者なし)'}\n新当番: **{new_actual_duty_person}**")
    else:
        await ctx.respond("現在アクティブなメンバーが見つからず、当番を更新できませんでした。", ephemeral=True)

@admin_group.command(name="当番設定", description="特定のアクティブなメンバーを今日の当番に設定します。")
@commands.has_permissions(administrator=True)
async def set_current_duty(ctx: discord.ApplicationContext, member_name: discord.Option(str, description="当番に設定するメンバー名", required=True)):
    load_data()
    today = datetime.datetime.now(JST).date()
    check_and_remove_expired_absentees(today)

    if member_name not in data["members"]:
        await ctx.respond(f"メンバー「{member_name}」は当番リストに登録されていません。", ephemeral=True)
        return
    if is_member_absent(member_name, today, data["absentees"]):
        await ctx.respond(f"メンバー「{member_name}」は本日欠席中のため、当番に設定できません。", ephemeral=True)
        return

    try:
        data["current_duty_index"] = data["members"].index(member_name)
        data["last_rotation_date"] = today.isoformat() # 設定日を記録
        save_data()
        await ctx.respond(f"**{member_name}** さんを本日の当番に設定しました。")
    except ValueError: # 万が一メンバーが見つからない場合（通常は上のチェックで弾かれる）
        await ctx.respond(f"エラー: メンバー「{member_name}」のインデックス取得に失敗しました。", ephemeral=True)


# --- 欠席管理コマンドグループ ---
absence_group = bot.create_group(
    name="欠席管理",
    description="メンバーの欠席情報を管理します。",
    guild_ids=guild_ids_list
)

@absence_group.command(name="登録", description="メンバーを欠席登録。終了日YYYY-MM-DD (任意、無ければ無期限)。")
@commands.has_permissions(administrator=True)
async def add_absence(ctx: discord.ApplicationContext,
                      member_name: discord.Option(str, description="欠席するメンバー名", required=True),
                      until_date_str: discord.Option(str, description="欠席終了日 (YYYY-MM-DD形式、この日まで欠席)", required=False)):
    load_data()
    if member_name not in data["members"]:
        await ctx.respond(f"メンバー「{member_name}」は当番リストに登録されていません。", ephemeral=True)
        return

    today_date = datetime.datetime.now(JST).date()
    since_date_str = today_date.isoformat()
    absence_info = {"since": since_date_str, "until": None}

    if until_date_str:
        try:
            parsed_until_date = datetime.datetime.strptime(until_date_str, "%Y-%m-%d").date()
            if parsed_until_date < today_date:
                 await ctx.respond("欠席終了日には過去の日付を指定できません。", ephemeral=True)
                 return
            absence_info["until"] = parsed_until_date.isoformat()
        except ValueError:
            await ctx.respond("終了日の日付形式が正しくありません。YYYY-MM-DD形式で入力してください。", ephemeral=True)
            return
    
    data["absentees"][member_name] = absence_info
    save_data()
    
    until_msg = f" ({absence_info['until']} まで)" if absence_info['until'] else " (無期限)"
    await ctx.respond(f"メンバー「{member_name}」を欠席登録しました{until_msg}。", ephemeral=True)

@absence_group.command(name="復帰", description="メンバーの欠席状態を解除（復帰）します。")
@commands.has_permissions(administrator=True)
async def remove_absence(ctx: discord.ApplicationContext, member_name: discord.Option(str, description="復帰するメンバー名", required=True)):
    load_data()
    if member_name in data["absentees"]:
        del data["absentees"][member_name]
        save_data()
        await ctx.respond(f"メンバー「{member_name}」を復帰させました（欠席リストから削除）。", ephemeral=True)
    else:
        await ctx.respond(f"メンバー「{member_name}」は欠席登録されていません。", ephemeral=True)

@absence_group.command(name="一覧", description="現在の欠席者リストを表示します。")
async def list_absentees(ctx: discord.ApplicationContext):
    load_data()
    today = datetime.datetime.now(JST).date()
    check_and_remove_expired_absentees(today) # 表示前に期限切れを処理

    if not data["absentees"]:
        await ctx.respond("現在、欠席登録されているメンバーはいません。")
        return
    
    message = "現在の欠席者リスト:\n"
    for member, info in data["absentees"].items():
        since_str = info.get('since', '不明')
        until_str = info.get('until')
        if until_str:
            message += f"- {member} (開始: {since_str}, 終了: {until_str})\n"
        else:
            message += f"- {member} (開始: {since_str}, 無期限)\n"
    await ctx.respond(message)


# --- 臨時活動日管理コマンドグループ ---
extra_activity_group = bot.create_group(
    name="臨時活動日", description="臨時の活動日を管理します。", guild_ids=guild_ids_list
)
@extra_activity_group.command(name="追加", description="臨時活動日を追加 (YYYY-MM-DD)。")
@commands.has_permissions(administrator=True)
async def add_extra_activity_day(ctx: discord.ApplicationContext, date_str: discord.Option(str, description="追加する日付 (YYYY-MM-DD)", required=True)):
    load_data()
    try:
        date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        iso_date_str = date_obj.isoformat()
        if iso_date_str not in data["extra_activity_days"]:
            data["extra_activity_days"].append(iso_date_str)
            data["extra_activity_days"].sort()
            save_data()
            await ctx.respond(f"臨時活動日 {iso_date_str} を追加しました。", ephemeral=True)
        else:
            await ctx.respond(f"臨時活動日 {iso_date_str} は既に登録されています。", ephemeral=True)
    except ValueError:
        await ctx.respond("日付の形式が正しくありません。YYYY-MM-DD形式で入力してください。", ephemeral=True)

@extra_activity_group.command(name="削除", description="臨時活動日を削除 (YYYY-MM-DD)。")
@commands.has_permissions(administrator=True)
async def remove_extra_activity_day(ctx: discord.ApplicationContext, date_str: discord.Option(str, description="削除する日付 (YYYY-MM-DD)", required=True)):
    load_data()
    try:
        date_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        iso_date_str = date_obj.isoformat()
        if iso_date_str in data["extra_activity_days"]:
            data["extra_activity_days"].remove(iso_date_str)
            save_data()
            await ctx.respond(f"臨時活動日 {iso_date_str} を削除しました。", ephemeral=True)
        else:
            await ctx.respond(f"臨時活動日 {iso_date_str} は登録されていません。", ephemeral=True)
    except ValueError:
        await ctx.respond("日付の形式が正しくありません。YYYY-MM-DD形式で入力してください。", ephemeral=True)

@extra_activity_group.command(name="一覧", description="登録されている臨時活動日を表示します。")
async def list_extra_activity_days(ctx: discord.ApplicationContext):
    load_data()
    if data["extra_activity_days"]:
        message = "登録されている臨時活動日:\n" + "\n".join(sorted(data["extra_activity_days"]))
        await ctx.respond(message)
    else:
        await ctx.respond("臨時活動日は登録されていません。")

# --- エラーハンドリング ---
@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: discord.DiscordException):
    if isinstance(error, commands.MissingPermissions):
        await ctx.respond("このコマンドを実行する権限がありません。", ephemeral=True)
    elif isinstance(error, commands.CommandOnCooldown):
        await ctx.respond(f"このコマンドはクールダウン中です。{error.retry_after:.2f}秒後にお試しください。", ephemeral=True)
    # ここに他の discord.py のエラータイプに対する処理を追加できる
    # 例: elif isinstance(error, commands.BadArgument): await ctx.respond("コマンドの引数が正しくありません。", ephemeral=True)
    else:
        # 予期せぬエラーのログを出力
        print(f"コマンド '{ctx.command.qualified_name if ctx.command else '不明'}' で予期せぬエラーが発生しました:")
        import traceback
        traceback.print_exc() # スタックトレースを出力

        try:
            if ctx.response.is_done():
                await ctx.followup.send("コマンド実行中に予期せぬエラーが発生しました。管理者に連絡してください。", ephemeral=True)
            else:
                await ctx.respond("コマンド実行中に予期せぬエラーが発生しました。管理者に連絡してください。", ephemeral=True)
        except discord.errors.InteractionResponded: # フォローアップも失敗した場合など
             print("エラー: InteractionResponded in error handler")
        except Exception as e_handler_error:
            print(f"エラーハンドリング中にさらにエラーが発生: {e_handler_error}")

# --- Botの実行 ---
if __name__ == "__main__":
    if TOKEN is None:
        print("エラー: DISCORD_BOT_TOKEN が設定されていません。環境変数を確認してください。")
        print("ローカルでテストする場合、.envファイルに DISCORD_BOT_TOKEN=YourTokenHere のように記述してください。")
    else:
        print(f"データファイルパス: {os.path.abspath(DATA_FILE)}")
        print(f"テストギルドID: {GUILD_ID if GUILD_ID else '未設定 (グローバルコマンド)'}")
        bot.run(TOKEN)