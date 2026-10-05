---
survey:
  id: social-platform-use
  version: 1
  title: Leading Social Platforms — Usage Study
  settings:
    url_variables:
    - uid
    - rid
    - group
    theme: paper
    default_redirect: default
    enrichments:
    - id: social_minutes_30d
      event_type: app_usage
      window:
        last: 30d
      criteria:
        field: app_category
        operator: equals
        value: Social
      operation: sum
      field: minutes
      type: number
    - id: top_social_app
      event_type: app_usage
      window:
        last: 30d
      criteria:
        field: app_category
        operator: equals
        value: Social
      operation: top
      field: app_name
      type: scalar
    - id: tiktok_minutes_30d
      event_type: app_usage
      window:
        last: 30d
      criteria:
        op: or
        conditions:
        - field: app_name
          operator: contains_any
          value:
          - tiktok
      operation: sum
      field: minutes
      type: number
    anti_fraud:
      attention_checks:
      - q_attention
      min_page_seconds: 2
      block_duplicate_uid: true
  quotas:
  - id: quota_overall
    scope: survey
    limit: 100
  pages:
  - id: p_intro
    questions:
    - id: q_consent
      type: radio
      required: true
      title: Thanks for stopping by! This is a quick (~3 minute) study about how people use today's leading
        social platforms. Do you consent to take part?
      options:
      - value: 'yes'
        label: Yes, I consent
      - value: 'no'
        label: 'No'
  - id: p_platforms
    questions:
    - id: q_platforms
      type: checkbox
      required: true
      title: Which of these social platforms do you currently use? (select all that apply)
      randomization:
        mode: shuffle
        fixed:
        - none
      options:
      - value: tiktok
        label: TikTok
      - value: instagram
        label: Instagram
      - value: facebook
        label: Facebook
      - value: youtube
        label: YouTube
      - value: x
        label: X (Twitter)
      - value: snapchat
        label: Snapchat
      - value: reddit
        label: Reddit
      - value: linkedin
        label: LinkedIn
      - value: none
        label: None of these
        exclusive: true
  - id: p_screentime
    display_logic:
      ref:
        kind: enrichment
        id: social_minutes_30d
      operator: greater_than
      value: 0
    questions:
    - id: q_screentime_confirm
      type: radio
      required: true
      title: Your device screentime over the last 30 days shows your most-used social app is {{behavior:top_social_app}}.
        Is that the platform you use most?
      options:
      - value: 'yes'
        label: Yes, that's the one
      - value: 'no'
        label: No, a different one
      - value: notsure
        label: Not sure
  - id: p_usage
    questions:
    - id: q_top_platform
      type: radio
      required: true
      title: Of the platforms you use, which do you spend the MOST time on?
      options_source:
        pipe_from: q_platforms
        mode: selected
        carry_values: true
  - id: p_usage_detail
    questions:
    - id: q_frequency
      type: radio
      required: true
      title: How often do you open {{q:q_top_platform.answer}}?
      options:
      - value: many_hour
        label: Many times an hour
      - value: few_day
        label: A few times a day
      - value: once_day
        label: About once a day
      - value: few_week
        label: A few times a week
      - value: rarely
        label: Less than weekly
    - id: q_value
      type: textarea
      required: false
      title: In a few words, what keeps you coming back to {{q:q_top_platform.answer}}?
  - id: p_attention
    questions:
    - id: q_attention
      type: radio
      required: true
      title: 'Attention check: to show you''re reading, please select ''Somewhat agree''.'
      options:
      - value: sd
        label: Strongly disagree
      - value: d
        label: Disagree
      - value: n
        label: Neither agree nor disagree
      - value: a
        label: Somewhat agree
      - value: sa
        label: Strongly agree
  actions:
  - type: disqualify
    reason_code: no_consent
    dq_message: No problem — thanks for stopping by.
    when:
      ref:
        kind: question
        id: q_consent
      operator: equals
      value: 'no'
  - type: disqualify
    reason_code: non_user
    dq_message: Thanks! This study is just for people who use social platforms.
    when:
      ref:
        kind: question
        id: q_platforms
      operator: contains
      value: none
  - type: disqualify
    reason_code: failed_attention
    dq_message: Thanks for your responses.
    when:
      ref:
        kind: question
        id: q_attention
      operator: not_equals
      value: a
  - type: overquota
    message: This study is now full — thank you!
    when:
      ref:
        kind: quota
        id: quota_overall
      operator: is_full
  - type: complete
    redirect:
      kind: url
      url: https://www.example.com/?uid={{url:uid}}&rid={{url:rid}}
    when:
      op: and
      conditions:
      - ref:
          kind: question
          id: q_attention
        operator: is_answered
      - op: or
        conditions:
        - ref:
            kind: question
            id: q_platforms
          operator: contains
          value: tiktok
        - ref:
            kind: enrichment
            id: tiktok_minutes_30d
          operator: greater_than
          value: 0
  - type: complete
    redirect:
      kind: default
---

# Leading Social Platforms — Usage Study

version 1

> Researcher-readable questionnaire with logic notes. The YAML frontmatter above is the **canonical source** — `survey-agent compile` regenerates the JSON from it. Edit the frontmatter (or the `.survey.json`), then re-render.

## Page `p_intro`

### 1. Thanks for stopping by! This is a quick (~3 minute) study about how people use today's leading social platforms. Do you consent to take part?  `radio`  *(required)*

<sub>id `q_consent`</sub>

- `yes` — Yes, I consent

- `no` — No

## Page `p_platforms`

### 2. Which of these social platforms do you currently use? (select all that apply)  `checkbox`  *(required)*

<sub>id `q_platforms`</sub>

- `tiktok` — TikTok

- `instagram` — Instagram

- `facebook` — Facebook

- `youtube` — YouTube

- `x` — X (Twitter)

- `snapchat` — Snapchat

- `reddit` — Reddit

- `linkedin` — LinkedIn

- `none` — None of these  _(exclusive)_

_Options shuffled; fixed: none._

## Page `p_screentime`

➡️ _Page shown only when context `social_minutes_30d` > 0._

### 3. Your device screentime over the last 30 days shows your most-used social app is {{behavior:top_social_app}}. Is that the platform you use most?  `radio`  *(required)*

<sub>id `q_screentime_confirm`</sub>

- `yes` — Yes, that's the one

- `no` — No, a different one

- `notsure` — Not sure

_Piped: `{{behavior:top_social_app}}`._

## Page `p_usage`

### 4. Of the platforms you use, which do you spend the MOST time on?  `radio`  *(required)*

<sub>id `q_top_platform`</sub>

- _options piped from Q `q_platforms` (selected)_

## Page `p_usage_detail`

### 5. How often do you open {{q:q_top_platform.answer}}?  `radio`  *(required)*

<sub>id `q_frequency`</sub>

- `many_hour` — Many times an hour

- `few_day` — A few times a day

- `once_day` — About once a day

- `few_week` — A few times a week

- `rarely` — Less than weekly

_Piped: `{{q:q_top_platform.answer}}`._

### 6. In a few words, what keeps you coming back to {{q:q_top_platform.answer}}?  `textarea`

<sub>id `q_value`</sub>

_Piped: `{{q:q_top_platform.answer}}`._

## Page `p_attention`

### 7. Attention check: to show you're reading, please select 'Somewhat agree'.  `radio`  *(required)*

<sub>id `q_attention`</sub>

- `sd` — Strongly disagree

- `d` — Disagree

- `n` — Neither agree nor disagree

- `a` — Somewhat agree

- `sa` — Strongly agree

## Quotas

- `quota_overall` — cap **100** (survey)

## Logic & terminals

- ⛔ **Screen out** (reason `no_consent`) — when Q `q_consent` is no · “No problem — thanks for stopping by.”

- ⛔ **Screen out** (reason `non_user`) — when Q `q_platforms` contains none · “Thanks! This study is just for people who use social platforms.”

- ⛔ **Screen out** (reason `failed_attention`) — when Q `q_attention` is not a · “Thanks for your responses.”

- 🪣 **Over-quota** terminal — when quota `quota_overall` is full

- ✅ **Complete** → https://www.example.com/?uid={{url:uid}}&rid={{url:rid}} — when (Q `q_attention` is answered AND (Q `q_platforms` contains tiktok OR context `tiktok_minutes_30d` > 0))

- ✅ **Complete** → default
