# Kidsnote for Home Assistant

키즈노트 알림장과 앨범을 주기적으로 받아 **Home Assistant의 `/media` 폴더**와 **Immich**에 백업하는 커스텀 통합입니다. 원하면 **HA 스크립트**로 다른 곳에도 보낼 수 있습니다.

*Backs up Kidsnote daily reports and albums into Home Assistant's media folder and Immich (with the report text as the asset description), and hands each new post to an optional HA script for any other destination.*

```
키즈노트 ──(1시간마다)──▶ /media/kidsnote/<아이>/<YYYY-MM>/<날짜>_<report|album>_<id>/
                           원본 사진·영상 + post.json, 파일 시각 = 게시 시각
                     ├──▶ Immich: 업로드 + 알림장 본문을 설명으로 + 아이별 앨범
                     └──▶ script.<전달 스크립트> (선택): 알림, Google Photos, OneDrive …
```

## 왜 아이디/비밀번호인가

키즈노트 `sessionid` 쿠키는 로그인 후 14일이면 만료됩니다. 쿠키만 저장하는 방식은 2주마다 끊길 수밖에 없습니다. 이 통합은 비밀번호를 이 HA 안에만 저장하고, 세션이 만료되면 알아서 다시 로그인합니다. 새로 로그인해도 휴대폰 앱이나 브라우저의 로그인은 유지됩니다. 비밀번호가 바뀌면 HA에 "다시 인증 필요" 알림이 뜹니다.

**키즈노트 2단계 인증이 켜져 있으면 자동 로그인이 안 됩니다.**

## 설치

HACS → 사용자 지정 저장소에 `https://github.com/hyungyunlim/ha-kidsnote`(유형: Integration)를 추가하고 설치한 뒤 HA를 재시작합니다. HACS를 쓰지 않는다면 `custom_components/kidsnote` 폴더를 `/config/custom_components/`로 복사합니다.

그다음 **설정 → 기기 및 서비스 → 통합 추가 → Kidsnote**를 선택합니다.

| 항목 | 설명 |
|---|---|
| 키즈노트 아이디 / 비밀번호 | 키즈노트 웹 로그인과 같은 계정 |
| Immich 주소 | 예: `http://192.168.0.10:2283`. 비우면 Immich 업로드를 하지 않음 |
| Immich API 키 | 권한: `asset.upload`, `asset.update`, `album.read`, `album.create`, `albumAsset.create` |
| 전달 스크립트 | 선택. 아래 [전달 스크립트](#전달-스크립트) 참고 |
| 확인 주기 | 기본 60분, 최소 15분 |

Immich 주소를 넣으면 다음 단계에서 **아이마다** 앨범을 고릅니다. 계정에 연결된 아이별로 선택 칸이 하나씩 나옵니다.

- **목록에서 기존 앨범 선택:** 앨범 ID로 저장합니다. 그래서 Immich에서 앨범 이름을 바꿔도 계속 같은 앨범에 넣습니다. 형제를 같은 앨범으로 고르면 한 앨범에 함께 들어갑니다.
- **`Kidsnote - {child}`(기본값) 또는 이름 직접 입력:** `{child}`를 아이 이름으로 바꾼 이름과 정확히 같은 앨범을 찾고, 없으면 새로 만듭니다.

### 아이가 여럿일 때

- **한 키즈노트 계정에 아이가 여럿이면** 통합 하나로 모두 받습니다. 폴더는 `/media/kidsnote/<아이 이름>/`으로 나뉘고, 전달 스크립트에는 `child` 변수로 넘어갑니다.
- **나중에 계정에 추가된 아이**는 다음 동기화부터 자동으로 받습니다. Immich 앨범은 `Kidsnote - {child}` 템플릿대로 만들어집니다. 다른 앨범을 쓰려면 **구성**에서 그 아이의 앨범을 고르세요.
- **아이마다 키즈노트 계정이 다르면** 통합을 계정 수만큼 추가하세요.

설정을 바꾸려면 통합의 **구성** 메뉴를 쓰면 됩니다.

## 처음 실행과 그 이후

- **처음 실행:** 아이별 알림장과 앨범을 최신 글부터 마지막 페이지까지 훑으며 전부 받습니다(백필). HA 시작을 막지 않도록 백그라운드에서 돌고, 중간에 끊겨도 다음 실행에서 이어 받습니다.
- **그 이후:** 매 주기마다 첫 페이지부터 보다가, 새 글이 하나도 없는 페이지를 만나면 멈춥니다. 보통은 첫 페이지 한 번만 조회합니다. 선생님이 최근 글에 사진을 추가한 경우에도 그 사진만 따로 받습니다.
- **이미 Immich에 있는 사진**(예: Social Archiver로 올린 것)은 내용이 같으면 Immich가 체크섬으로 같은 파일로 인식합니다. 그래서 중복으로 올라가지 않고 기존 자산에 설명과 앨범만 맞춰집니다. 다만 체크섬을 계산하려면 원본을 한 번은 내려받아야 합니다.
- 보호자가 원에 보낸 알림장(보호자 본인 사진)은 받지 않습니다.
- 지금 바로 받으려면 `kidsnote.sync` 액션을 실행하세요.

`sensor.kidsnote_<계정>_last_sync`: 마지막으로 성공한 시각. 속성으로 `syncing`, `last_error`, `last_delivered`, `backfill_done`을 제공합니다.

## 전달 스크립트

스크립트를 지정하면, 새 게시물(또는 기존 게시물에 새로 붙은 사진)마다 그 스크립트를 호출하고 끝날 때까지 기다립니다. 스크립트가 **마지막에 `{ok: true}`를 반환해야** 전달된 것으로 기록합니다. 실패하거나 중간에 멈추면 다음 주기에 다시 호출합니다.

| 변수 | 내용 |
|---|---|
| `child`, `kind`, `id` | 아이 이름, `report`(알림장)/`album`(앨범), 게시물 id |
| `date`, `title`, `text`, `author` | 게시 시각(ISO), 제목, 본문, 작성자 |
| `files` | 이번에 받은 파일의 media-source id 목록 (`media-source://media_source/local/kidsnote/...`) |
| `paths` | 같은 파일의 절대 경로 목록 (`/media/kidsnote/...`) |
| `backfill` | 처음 실행(백필) 중이면 `true` |
| `new` | 처음 보는 게시물이면 `true`, 기존 게시물에 사진이 추가된 경우면 `false` |

```yaml
script:
  kidsnote_deliver:
    alias: Kidsnote 전달
    sequence:
      - if: "{{ new and not backfill }}"
        then:
          - action: notify.mobile_app_my_phone
            data:
              title: "키즈노트 · {{ child }}"
              message: "{{ (text or title)[:120] }}"
      # 예: Google Photos에도 올리기
      # - action: google_photos.upload
      #   data:
      #     config_entry_id: <google_photos entry id>
      #     filename: "{{ paths }}"
      #     album: Kidsnote
      - variables:
          result: { ok: true }
      - stop: done
        response_variable: result
```

## 참고

- 키즈노트가 공식적으로 공개한 API가 아니라 kidsnote.com 웹이 내부에서 쓰는 API를 사용합니다. 키즈노트가 구조를 바꾸면 동작하지 않을 수 있습니다.
- 비밀번호는 HA의 `.storage/core.config_entries`에 다른 통합의 자격 증명과 같은 방식으로 저장됩니다.
- 받은 파일은 `/media/kidsnote`에 남습니다. HA 백업 크기가 걱정되면 백업에서 Media 폴더를 빼세요.
- 백필이 끝난 뒤에는 첫 페이지보다 오래된 글이 수정돼도 다시 확인하지 않습니다.
- 동기화 로직 테스트: `python3 tests/test_sync.py`
