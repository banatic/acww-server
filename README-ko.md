# ACWW 온라인 서버 (`acww-online`)

## 이게 뭔가요

PC로 포팅한 《동물의 숲 - 튀어나와요 동물의 숲》(한국판 ADMK)을 **여러 PC에서 같은 마을로**
플레이하게 해 주는 작은 서버입니다. 하는 일은 네 가지뿐입니다.

1. **계정** — 아이디와 비밀번호. 비밀번호는 argon2id로 해시해서 저장하고, 원문은 어디에도
   남기지 않습니다.
2. **클라우드 세이브** — 카트리지 플래시 이미지 그대로인 262,144바이트(256 KB) 파일을
   계정별로 보관합니다. 어느 PC에서 켜도 같은 마을이 뜹니다. 최근 **20개 버전**을 남기므로
   실수로 덮어썼을 때 되돌릴 수 있습니다.
3. **로비** — 같이 놀 사람을 기다리는 대기실. 목록에서 서로를 보고 초대/수락합니다.
4. **릴레이** — 매칭된 두 사람 사이에서 게임의 무선통신 데이터를 **내용을 들여다보지 않고**
   그대로 전달합니다.

**ROM이나 게임 데이터는 서버에 전혀 들어가지 않습니다.** 이미지 안에는 파이썬 코드와
라이브러리 6개뿐이고, 서버가 다루는 게임 관련 바이트는 사용자 본인의 세이브 파일뿐입니다.

## 시놀로지 Container Manager 설치 순서

1. **File Station**에서 데이터 폴더를 만듭니다. 예: `/docker/acww-online/data`.
   그 위 폴더(`/docker/acww-online`)에 `docker-compose.yml`을 올려 둡니다.
2. **SSH로 한 번만** 소유자를 맞춰 줍니다. 컨테이너는 보안을 위해 root가 아닌
   **uid 10001**로 돌아가므로, 이 줄을 실행하지 않으면 서버가 데이터 폴더에 쓰지 못합니다.

   ```
   sudo chown -R 10001:10001 /volume1/docker/acww-online/data
   ```

3. **Container Manager → 프로젝트 → 생성**. 경로는 `/docker/acww-online`,
   소스는 "docker-compose.yml 업로드" 또는 "기존 docker-compose.yml 사용".
4. 같은 화면의 환경 변수 칸에 **`ACWW_SERVER_SECRET`** 을 넣습니다. 값은 아무나 못 맞출 긴
   무작위 문자열이면 됩니다(64자 16진수 권장). 예시 — **이 값을 그대로 쓰지 마세요**:

   ```
   ACWW_SERVER_SECRET=여기에_직접_만든_64자리_무작위_문자열을_넣으세요
   ```

   비워 두면 서버가 첫 실행 때 하나를 만들어 `data/secret.key`에 저장하고 로그에 그 사실을
   남깁니다. 그것도 괜찮지만, **그 파일이 사라지면 모든 로그인 토큰이 무효가 됩니다.**
   백업에 `data` 폴더 전체가 들어가는지 확인하세요.
5. **빌드 → 실행.** 로그 탭에 `Uvicorn running on http://0.0.0.0:8080` 이 보이면 성공입니다.
6. **역방향 프록시 + TLS.** 제어판 → 로그인 포털 → 고급 → 역방향 프록시 → 생성.
   - 소스: HTTPS, 원하는 도메인(예: `acww.내도메인.kr`), 포트 443
   - 대상: HTTP, `localhost`, 포트 8080
   - **연결 → WebSocket 지원을 반드시 켜세요.** 로비와 릴레이가 WebSocket입니다.
   - 인증서는 제어판 → 보안 → 인증서에서 Let's Encrypt로 발급해 이 도메인에 연결합니다.
   - 서버 자체는 평문 HTTP만 말합니다. **8080 포트를 공유기에서 외부로 열지 마세요.**
     바깥으로 나가는 문은 NAS의 443 하나여야 합니다.
7. 클라이언트(PC의 `acww-online.ini`)의 `server=` 에는 8080이 아니라 프록시 주소를
   적습니다: `server=https://acww.내도메인.kr`

## 계정을 다 만든 뒤에는 가입을 닫으세요

`docker-compose.yml`의 `ACWW_ALLOW_REGISTER` 를 `0` 으로 바꾸고 프로젝트를 다시 빌드/실행하면
그때부터 새 가입은 403으로 거절됩니다. 이미 만든 계정은 그대로 로그인됩니다.
**인터넷에 열어 두는 서버라면 이 단계를 건너뛰지 마세요.**

## 환경 변수

| 변수 | 기본값 | 뜻 |
|---|---|---|
| `ACWW_SERVER_SECRET` | (없음 → 자동 생성) | 로그인 토큰 서명 키. 바꾸면 모든 토큰이 즉시 무효가 됩니다(= 강제 로그아웃) |
| `ACWW_ALLOW_REGISTER` | `1` | `0`이면 신규 가입 금지 |
| `ACWW_TRUSTED_PROXIES` | (없음) | **역방향 프록시의 IP 주소.** 이 값에 적힌 주소에서 온 요청만 `X-Forwarded-For` 헤더를 믿습니다. 비워 두면 헤더를 아예 읽지 않습니다(권장 기본값). 예: `127.0.0.1` 또는 `172.16.0.0/12` |
| `ACWW_TRUST_PROXY` | `0` | 옛 이름입니다. 이제 이것만 켜도 아무것도 믿지 않고, 로그에 경고 한 줄이 남습니다. 위의 `ACWW_TRUSTED_PROXIES` 를 쓰세요 |
| `ACWW_DATA_DIR` | `/data` | 데이터 위치. 건드릴 일 없습니다 |
| `ACWW_SAVE_HISTORY` | `20` | 계정당 보관할 세이브 버전 수 |
| `ACWW_TOKEN_DAYS` | `30` | 로그인 유효 기간(일) |
| `ACWW_AUTH_RATE_LIMIT` / `_WINDOW` | `10` / `60` | IP당 60초에 로그인·가입 시도 10번 |
| `ACWW_INVITE_TTL` | `60` | 보낸 초대가 유효한 시간(초). 지나면 수락해도 거절됩니다 |
| `ACWW_MAX_LOBBY_SOCKETS` / `ACWW_MAX_ROOMS` | `32` / `16` | 서버 한 대가 동시에 들고 있을 로비 연결 수와 통신 중인 쌍의 수 |

**나머지 상한값은 손댈 일이 없습니다.** 요청 크기·비밀번호 길이·초당 메시지 수 같은 것들은
`server/API.md` 의 SERVERFIX108 표에 전부 적혀 있고, 기본값은 **게임이 실제로 만드는 통신량보다
훨씬 여유롭게** 잡혀 있습니다(세이브 왕복 + 로비 대화 + 초당 60프레임 릴레이 5초치를 통과시키는
것을 시험으로 확인합니다). 값을 낮추면 게임이 멈출 수 있습니다.

## 살아 있는지 확인하기

브라우저나 터미널에서:

```
curl https://acww.내도메인.kr/v1/health
{"ok":true,"version":"1.0.0","commit":"<40자리 커밋 해시>","users":2,"waiting":0,"rooms":0}
```

`users`는 계정 수, `waiting`은 지금 로비에서 기다리는 사람, `rooms`는 연결된 쌍의 수입니다.
Container Manager의 상태 표시(healthcheck)도 30초마다 같은 주소를 확인합니다.

`commit`은 빌드에 포함된 소스의 커밋입니다. PC의 post-commit 훅이
`server/app/build_commit.txt`를 자동 생성하며, NAS에는 **이 파일까지 포함해 server 폴더를
복사**하고 평소처럼 다시 빌드하면 됩니다. 빌드 인자나 NAS의 Git 설치는 필요 없습니다.
브라우저에서 https://acww.moominda.synology.me/v1/health 를 열어 PC의 `git rev-parse HEAD`와
비교하세요. 응답에는 캐시 방지를 위한 `Cache-Control: no-store`가 포함됩니다.

새 체크아웃에서는 `git config core.hooksPath .githooks`로 훅을 활성화하세요.
필요하면 저장소 루트에서 `python tools/stamp_server_commit.py`로 다시 생성할 수 있습니다.
이 파일은 자기 커밋의 해시를 담으므로 Git 추적에서 제외합니다. 따라서 Git clone/archive로만
전달하면 포함되지 않습니다. 파일이 없거나 잘못된 값이면 `commit`은 `unknown`입니다.
생성 시 서버에 미커밋 변경이 있어도 `unknown`으로 기록합니다. 커밋 후 서버 코드를 추가로
수정했다면 복사 전에 다시 생성하세요. 실행 환경변수로 커밋 표시를 덮어쓸 수는 없습니다.

## 비밀번호를 잊었을 때

이 서버에는 이메일도 재설정 링크도 없습니다. **NAS에 접근할 수 있는 사람이 곧 재설정할 수
있는 사람**입니다. SSH에서:

```
sudo docker exec -it acww-online python -m app.cli set-password <아이디>
```

새 비밀번호를 두 번 물어봅니다(화면에 찍히지 않습니다). 8자 이상이어야 합니다.
계정 목록은 `sudo docker exec -it acww-online python -m app.cli list-users`,
보관된 세이브 파일이 게임에서 열리는지 검사하려면
`sudo docker exec -it acww-online python -m app.cli check-save /data/saves/1/3.sav`.

## 백업

백업해야 할 것은 **`data` 폴더 하나**입니다. 그 안에:

- `acww.sqlite` — 계정과 세이브 버전 기록
- `saves/<계정번호>/<버전>.sav` — 세이브 파일 원본(각 262,144바이트)
- `secret.key` — 자동 생성된 서명 키(환경 변수로 직접 넣었다면 없습니다)

Hyper Backup으로 이 폴더를 통째로 잡아 두면 됩니다. 컨테이너를 잠깐 멈추고 복사하면 더
안전하지만, 저장 방식상 켜진 채로 복사해도 세이브 파일이 반쯤 쓰인 상태로 잡히지는 않습니다.

## 안전에 관한 메모 (읽어 주세요)

- **클라이언트는 서명되지 않은 프로그램입니다.** 서버는 접속한 쪽이 진짜 게임인지 확인할 수
  없습니다. 확인하는 것은 오직 "이 토큰이 이 계정의 것인가"뿐입니다. 그래서 **가입을 닫는
  것**과 **비밀번호를 길게 쓰는 것**이 실질적인 방어선입니다.
- **서명 키(HS256)는 이 서버의 마스터 키입니다.** 이 값을 아는 사람은 아무 계정의 토큰이든
  만들어 낼 수 있습니다. `docker-compose.yml`에 직접 적어서 git에 올리지 마세요 — 파일은
  환경 변수만 참조하도록 되어 있습니다.
- **서버에는 ROM도, 게임에서 추출한 데이터도 없습니다.** 세이브 파일은 게임 데이터가 아니라
  사용자가 플레이해서 만든 본인의 기록입니다.
- **업로드된 세이브는 게임의 검사를 그대로 통과해야만 저장됩니다.** ROM이 직접 쓰는 검사
  (게임코드 바이트, `+0x173fa` 플래그, 뱅크 전체의 16비트 합)를 뱅크 1·2 양쪽에 돌려서,
  게임이 못 여는 파일은 400으로 거절하고 이유를 알려 줍니다. 고장난 파일이 올라가서 멀쩡한
  마을을 덮어쓰는 일을 막기 위한 것입니다.
- **로그에는 비밀번호도, 토큰도, 세이브의 내용도 남지 않습니다.** 남는 것은 계정 번호,
  파일 크기, sha256 값뿐입니다. — 2026-09-12(SERVERFIX108)까지는 이 문장이 **반만 맞았습니다.**
  서버 자신의 로그에는 토큰이 없었지만, 그 아래에서 돌아가는 웹서버(uvicorn)가 로비·릴레이
  접속을 기록할 때 **주소 뒤에 붙은 `?token=...` 을 그대로 찍고 있었습니다.** 그 토큰은 30일
  동안 다시 쓸 수 있는 열쇠라서, 로그를 내보내거나 백업한 사람은 그 계정으로 로그인할 수
  있었습니다. 지금은 (1) 게임이 토큰을 주소가 아니라 **요청 헤더**로 보내고, (2) 그래도 로그에
  들어오는 주소의 `?` 뒷부분은 `<redacted>` 로 지웁니다. **예전 로그를 보관하고 있다면
  지우거나, `ACWW_SERVER_SECRET` 을 새 값으로 바꿔(=모두 강제 로그아웃) 옛 토큰을 무효로
  만드세요.**
- **초대를 받지 않은 사람은 나를 매칭할 수 없습니다(2026-09-12에 고침).** 전에는 로비 목록에
  있는 사람이 초대 없이 "수락"만 보내도 둘이 묶여 버렸습니다. 이제 서버가 **누가 누구를
  초대했는지**를 60초 동안 기억하고, 그 초대가 없으면 수락을 거절합니다.
- **8080을 공유기에서 직접 열지 마세요.** 평문입니다. 반드시 NAS 역방향 프록시의 HTTPS를
  거치게 하세요.

## 개발자용

```
python -m pytest server/tests -q          # 98개 테스트
docker build -t acww-online:dev server/
docker run -d -p 18080:8080 -v <폴더>:/data acww-online:dev
python server/tools/smoke.py http://127.0.0.1:18080
```

API 명세는 `server/API.md`, 원본 계약은 `docs/kb/hybrid/online-spec.md`,
구조와 위협 모델은 `wiki/systems/online-server.md`.

## 리버스 프록시와 HTTPS (INTEGRATE83에서 실제로 확인한 설정)

이 서비스는 **평문 HTTP 한 포트**만 씁니다. TLS는 앞단의 리버스 프록시(시놀로지의 역방향
프록시, nginx, Caddy 등) 몫입니다. 여기서 중요한 것은 **웹소켓**입니다. 아래 세 가지를
빠뜨리면 **클라우드 세이브는 되는데 로비만 조용히 안 되는** 가장 나쁜 형태로 고장 납니다.

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

server {
    listen 443 ssl;
    server_name nas.example.com;
    ssl_certificate     /etc/nginx/tls/cert.pem;
    ssl_certificate_key /etc/nginx/tls/key.pem;
    client_max_body_size 4m;          # 세이브 한 개가 256 KB입니다

    location / {
        proxy_pass http://127.0.0.1:18080;
        proxy_http_version 1.1;                        # (1) 1.0으로는 업그레이드가 안 됩니다
        proxy_set_header Upgrade $http_upgrade;        # (2)
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;                      # (3) 로비 소켓은 원래 조용합니다
        proxy_send_timeout 3600s;
    }
}
```

`Connection` 을 `upgrade` 로 고정하면 안 됩니다 — 일반 요청까지 망가집니다. 그래서 `map`
입니다. `proxy_read_timeout` 의 기본값 60초는 기다리는 중인 사람의 로비 소켓을 끊어 버려서
"서버가 나를 튕겼다"처럼 보입니다.

**프록시 뒤에서는 `ACWW_TRUSTED_PROXIES` 에 프록시의 IP를 적으세요.** 적지 않으면 로그인 시도
제한이 모든 사람을 프록시 한 대로 보고 셉니다. 시놀로지 역방향 프록시라면 보통 `127.0.0.1`,
도커 네트워크를 거치면 `172.16.0.0/12` 입니다.

**그리고 위 nginx 설정에서 `X-Forwarded-For` 줄을 한 글자 바꾸는 편이 더 안전합니다.**

```nginx
        proxy_set_header X-Forwarded-For $remote_addr;   # $proxy_add_x_forwarded_for 가 아니라
```

`$proxy_add_x_forwarded_for` 는 **바깥에서 보내온 헤더를 그대로 남겨 두고** 그 뒤에 진짜
접속자를 덧붙입니다. 그래서 누군가 이 헤더를 직접 만들어 보내면 서버가 보는 "접속자 목록"의
앞쪽은 그 사람이 지어낸 값입니다. `$remote_addr` 로 **덮어쓰면** 그 줄에는 프록시가 실제로 본
주소 하나만 남습니다. (서버는 두 방식 모두에서 올바르게 동작합니다 — 목록의 오른쪽부터 읽어서
프록시가 아닌 첫 주소를 고릅니다. 덮어쓰기는 그보다 한 단계 더 확실한 쪽입니다.)

**인증서가 자체 서명(직접 만든 것)이라면** 플레이어의 PC가 그것을 믿지 않아서 접속이
실패합니다. 게임은 멈추지 않고 오프라인으로 진행하며 로그에 한 줄 남깁니다. 해결은 두
가지이고, 순서가 있습니다.

1. **인증서를 각 PC가 믿게 합니다(권장).** 관리자 권한에서 `certutil -addstore -f Root
   cert.pem`, 되돌릴 때 `certutil -delstore Root <이름>`.
2. `acww.exe --insecure-tls` — **시험용입니다.** 서버 인증서를 전혀 확인하지 않습니다.
   명령줄로만 줄 수 있고(ini에 적을 수 없습니다), 쓴 실행마다 로그에 그 사실을 남깁니다.
   집 안에서 한 번 확인해 볼 때만 쓰고, 평소에는 1번으로 두세요.

둘 다 nginx + 자체 서명 인증서 앞에서 실제로 확인했습니다(로그인, 세이브 내려받기·올리기,
로비 웹소켓 전부). 재현 방법은 `python port/tools/test_online_integration.py --only
accounts,tls`.

## 클라이언트와 함께 돌려 보기

```
python port/tools/test_online_integration.py
```

이 이미지를 빌드해서 **새 볼륨**으로 띄우고, `dist/acww.exe` 를 실제로 실행해 계정 가입,
로그인, 세이브 왕복, 412 충돌, 오프라인, **두 개의 게임 프로세스가 로비에서 만나는 것**,
그리고 TLS까지 7단계를 확인합니다. 도커가 없거나 exe가 없으면 그냥 건너뜁니다(exit 0).
`server/tools/smoke.py` 는 그대로이고, 서버만 확인하고 싶을 때 쓰는 빠른 쪽입니다.

## 서버가 옛 버전이라면 (`ws_query_token`)

2026-09-12(SERVERFIX108)부터 게임은 로비·릴레이 접속의 로그인 토큰을 **요청 헤더**로 보냅니다.
그보다 **오래된 서버 이미지**는 주소에 붙은 `?token=` 만 읽으므로, 새 게임 + 옛 서버 조합에서는
**세이브는 되는데 로비만 안 되는** 상태가 됩니다. 서버를 다시 빌드하는 것이 정답이지만, 급할
때는 게임 쪽에서 옛 방식을 한 번만 되살릴 수 있습니다. `acww-online.ini` 에 한 줄:

```
ws_query_token=1
```

또는 환경 변수 `ACWW_ONLINE_WS_QUERY_TOKEN=1`. **임시 수단입니다** — 토큰이 다시 주소에
실리므로 옛 서버의 로그에 남을 수 있고, 게임은 이 줄을 쓴 실행마다 로그에 경고를 남깁니다.
다음 판에서는 이 길이 없어집니다. 서버를 올리고 나면 줄을 지우세요.
