# One-time setup: stores the keys as GitHub secrets (they never go into the code or the chat).
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName Microsoft.VisualBasic
Add-Type -AssemblyName System.Windows.Forms

$Repo = "alfandari123/otc-alerts"
$Title = "הגדרת בוט התראות OTC"

function Ask($text) {
    return [Microsoft.VisualBasic.Interaction]::InputBox($text, $Title, "").Trim()
}

function Say($text, $icon = "Information") {
    [System.Windows.Forms.MessageBox]::Show($text, $Title, "OK", $icon, "Button1", "RtlReading, RightAlign") | Out-Null
}

function SetSecret($name, $value) {
    gh secret set $name --repo $Repo --body $value | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "לא הצלחתי לשמור את $name ב-GitHub" }
}

try {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw "התוכנה gh (GitHub) לא נמצאה במחשב" }

    # Step 1: Telegram bot token
    while ($true) {
        $tg = Ask "שלב 1 מתוך 3`n`nהדבק כאן את הטוקן שקיבלת מ-BotFather בטלגרם`n`n(נראה בערך כך: 123456789:ABCdef...)"
        if (-not $tg) { exit }
        try {
            $me = Invoke-RestMethod "https://api.telegram.org/bot$tg/getMe"
            $botName = $me.result.username
            break
        } catch {
            Say "הטוקן לא תקין. העתק אותו שוב מ-BotFather ונסה שוב." "Warning"
        }
    }

    # Step 2: Google AI key (optional)
    while ($true) {
        $gm = Ask "שלב 2 מתוך 3`n`nהדבק כאן את מפתח ה-AI מ-Google AI Studio`n`n(מתחיל ב-AIza...)`n`nאפשר להשאיר ריק ולהוסיף אחר כך"
        if (-not $gm) { break }
        try {
            Invoke-RestMethod "https://generativelanguage.googleapis.com/v1beta/models?key=$gm" | Out-Null
            break
        } catch {
            Say "המפתח לא תקין. העתק אותו שוב ונסה שוב." "Warning"
        }
    }

    # Step 3: contact e-mail required by the SEC
    while ($true) {
        $mail = Ask "שלב 3 מתוך 3`n`nהאימייל שלך`n`nרשות ניירות הערך האמריקאית (SEC) מחייבת כתובת ליצירת קשר מכל מי שקורא את הנתונים שלה.`nהכתובת נשמרת מוסתרת ולא מופיעה באתר."
        if (-not $mail) { exit }
        if ($mail -match "^[^@\s]+@[^@\s]+\.[^@\s]+$") { break }
        Say "כתובת האימייל לא תקינה. נסה שוב." "Warning"
    }

    SetSecret "TELEGRAM_TOKEN" $tg
    if ($gm) { SetSecret "GEMINI_API_KEY" $gm }
    SetSecret "SEC_CONTACT" $mail
    gh workflow run run.yml --repo $Repo | Out-Null

    Say "הכל מוכן!`n`nעכשיו ייפתח הבוט שלך בטלגרם (@$botName).`nלחץ שם על START.`n`nאחר כך שלח לו את המניות שלך, למשל:`n/add ABCD EFGH`n`nהבוט עונה תוך כמה דקות."
    Start-Process "https://t.me/$botName"
} catch {
    Say "משהו השתבש:`n$($_.Exception.Message)`n`nצלם את ההודעה הזו ושלח ל-Claude." "Error"
}
