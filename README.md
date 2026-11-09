# Ubuntu Monitor

Мониторинг Ubuntu-сервера с уведомлениями в Telegram через внешний HTTP relay.
Проверяет CPU, RAM, swap, свободное место и inode; сообщает о перезагрузке,
проблемах, восстановлении и сохраняющихся проблемах. Проверка запускается
таймером systemd примерно каждые 5 минут. Входящие порты не нужны.

Требования: Ubuntu с systemd, Python 3.10+, `git`, `python3-venv`, доступ к PyPI
и настроенному relay по сети. Relay — отдельный сервис, в этот проект не входит.
Для установки нужны права sudo. Git-команды выполняются обычным пользователем.

## Установка на новый сервер

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv
git clone https://github.com/infoman2001/ubuntu-monitor.git ~/ubuntu-monitor
cd ~/ubuntu-monitor
sudo bash scripts/install.sh
```

Первый запуск создаст `/opt/ubuntu-monitor/.env` с правами `600` и завершится,
чтобы вы могли указать настройки:

```bash
sudo nano /opt/ubuntu-monitor/.env
sudo bash scripts/install.sh
sudo systemctl start ubuntu-monitor.service
sudo journalctl -u ubuntu-monitor.service -n 50 --no-pager
```

Обязательные параметры: `RELAY_URL`, `RELAY_AUTH`, `TG_TOKEN`, `TG_CHAT_ID`.
Если `SERVER_NAME` пуст, используется hostname сервера. Остальные параметры
и значения по умолчанию приведены в [.env.example](.env.example).
Файл `.env` — простой формат `KEY=value`, он не исполняется как shell-скрипт.

Relay получает POST с JSON `{"text": "...", "chat_id": "..."}` и заголовками
`X-Relay-Auth`, `X-TG-Token`. Успех — HTTP 2xx; JSON с `ok:false` или
`success:false` считается ошибкой доставки. Используйте HTTPS.

## Обновление до последней версии main

```bash
cd ~/ubuntu-monitor
bash scripts/update.sh
```

Скрипт запускается под владельцем клона, например `infoman`, без `sudo` перед
всей командой. Он выполняет fetch, проверяет отсутствие локальных изменений,
получает `origin/main` только через fast-forward и вызывает установку с sudo.
Для публичного репозитория достаточно HTTPS-клона: ключ GitHub для обновлений
не требуется. Обновление выполняется вручную; автоматического обновления по
расписанию нет.

Установщик создаёт отдельный каталог версии с venv в
`/opt/ubuntu-monitor/releases/`, устанавливает зависимости, проверяет импорт
и только затем останавливает таймер и переключает `current`. При ошибке
переключения восстанавливаются предыдущий `current`, unit-файлы и состояние
таймера. Сбой скачивания зависимостей не останавливает работающую версию.
`.env`, `state.json` и `monitor.lock` сохраняются на месте.

После установки проверка выполняется таймером. Установщик проверяет окружение
и запуск таймера, но не проверяет доставку в Telegram. Для проверки доставки
запустите service вручную и посмотрите журнал. Отсутствие уведомления само по
себе нормально: при штатных показателях повторные сообщения не отправляются.
Если установка завершилась ошибкой, исправьте причину и повторите update:
установка выполняется и при неизменившемся Git-коммите.

Git-клон хранится отдельно от рабочей установки. Если локальная ветка содержит
собственные коммиты или изменения, обновление останавливается без их удаления.
Сохраните изменения, отправьте их в GitHub или разрешите расхождение вручную.

## Переход с прежней установки

Если монитор уже находится в `/opt/ubuntu-monitor`, не клонируйте Git поверх
него. Создайте клон в `~/ubuntu-monitor` и выполните установщик из клона.
Он использует существующий `.env` и `state.json`, сохраняет прежние `monitor.py`
и `venv` и меняет systemd на запуск через `current`. До запуска установщика
действующая установка продолжает работать.

## Управление и откат

```bash
sudo systemctl list-timers ubuntu-monitor.timer
sudo systemctl status ubuntu-monitor.timer
sudo systemctl start ubuntu-monitor.service
sudo journalctl -u ubuntu-monitor.service -f
sudo systemctl disable --now ubuntu-monitor.timer  # приостановить проверки
sudo cat /opt/ubuntu-monitor/current/REVISION      # Git-коммит версии
```

Для ручного отката после успешной установки:

```bash
sudo ls -lt /var/backups/ubuntu-monitor/
# Выберите каталог нужного обновления:
sudo cat /var/backups/ubuntu-monitor/UPDATE_ID/previous-release
sudo systemctl stop ubuntu-monitor.timer ubuntu-monitor.service
# Если previous-release НЕ пуст, подставьте прочитанный путь:
sudo ln -sfn /opt/ubuntu-monitor/releases/PREVIOUS_RELEASE /opt/ubuntu-monitor/current.next
sudo mv -Tf /opt/ubuntu-monitor/current.next /opt/ubuntu-monitor/current
sudo cp /var/backups/ubuntu-monitor/UPDATE_ID/ubuntu-monitor.service /etc/systemd/system/
sudo cp /var/backups/ubuntu-monitor/UPDATE_ID/ubuntu-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ubuntu-monitor.timer
```

При откате первой миграции `previous-release` пуст: пропустите команды замены
ссылки, восстановите оба старых unit-файла из backup и перезагрузите systemd.
Они снова запустят прежние `monitor.py` и `venv`.
Старые версии и backup unit-файлов не удаляются автоматически. Удаляйте их
вручную после проверки новой версии, сохраняя текущую и нужную для отката.
Не удаляйте `state.json` при обычных обновлениях.

## Особенности мониторинга

- CPU измеряется за одну секунду; последовательные проверки снижают число
  случайных уведомлений, но не измеряют среднюю нагрузку за все 5 минут.
- `DISK_PATHS=/,/data` задаёт пути. Если они на одном разделе, метрики совпадут.
- Порог места в GB и процентах объединён через OR; `0` отключает этот порог.
- При ошибке отправки подтверждённые флаги оповещения не меняются, проблемное
  уведомление повторяется на следующей проверке. При потере ответа relay
  возможен дубль уже доставленного сообщения.
- При первом запуске отправляется сообщение о старте; смена boot ID вызывает
  сообщение о перезагрузке. Монитор не обнаружит отключение собственного хоста
  до его восстановления — для этого нужен внешний контроль доступности.

## Разработка

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
bash -n scripts/install.sh scripts/update.sh
shellcheck scripts/*.sh  # sudo apt-get install shellcheck
```

Тесты установки работают во временных каталогах с имитацией systemd, venv и
pip. Тесты обновления используют локальный Git remote и имитацию sudo.
Они проверяют сохранение конфигурации и состояния, откат при ошибках и
защиту локальных изменений. GitHub Actions запускает эти проверки и проверку
реальных Python-зависимостей на Python 3.10, 3.12 и 3.13.

В Git не должны попадать `.env`, state, ключи, venv и логи. Публикуются только
исходники и шаблон конфигурации. Лицензия пока не выбрана; перед использованием
проекта другими людьми добавьте подходящий LICENSE.
