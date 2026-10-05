---
survey:
  id: piping-showcase
  version: 1
  title: Piping Showcase
  description: 'Worked example of every piping form: merge codes (label vs value, multi-select lists,
    grid cells, URL values, calculated values), fallbacks for answers that may be missing, and option
    piping (selected / not selected), including the cases that used to go wrong.'
  tags:
  - example
  - piping
  settings:
    url_variables:
    - uid
    - rid
    - src
    theme: paper
    default_redirect: default
  hidden:
  - id: n_services
    calc:
      op: count_selected
      ref:
        kind: question
        id: q_services
  pages:
  - id: p_intro
    questions:
    - id: q_hello
      type: instructions
      title: Thanks for joining from {{url:src|the web}}
      body: This short study shows how answers are carried forward. Add `&src=email` to the link to see
        the URL value change.
    - id: q_name
      type: text
      title: What should we call you? (optional)
  - id: p_services
    title: Hi {{q:q_name.answer|there}}
    questions:
    - id: q_services
      type: checkbox
      required: true
      title: Which streaming services do you pay for?
      options:
      - value: netflix
        label: Netflix
      - value: prime
        label: Prime Video
      - value: disney
        label: Disney+
      - value: max
        label: Max
      - value: hulu
        label: Hulu
      - value: none
        label: None of these
        exclusive: true
    - id: q_tried
      type: checkbox
      title: Which have you tried in the past but don't pay for now? (optional)
      options:
      - value: netflix
        label: Netflix
      - value: prime
        label: Prime Video
      - value: disney
        label: Disney+
      - value: max
        label: Max
      - value: hulu
        label: Hulu
  - id: p_fav
    title: '{{var:n_services}} service(s) — {{q:q_services.answer}}'
    questions:
    - id: q_fav
      type: radio
      required: true
      title: Of the ones you pay for, which is your favourite?
      body: '_Option piping (selected): only what you picked, and never "None of these"._'
      options_source:
        pipe_from: q_services
        mode: selected
    - id: q_next
      type: radio
      title: Which one you don't have would you most consider adding?
      body: _Option piping (not selected). Skipped automatically if you picked them all._
      options_source:
        pipe_from: q_services
        mode: not_selected
    - id: q_tried_best
      type: radio
      title: Of the ones you tried before, which did you like best?
      body: '_Piped from an optional question: skipped, not shown empty, if you left that blank._'
      options_source:
        pipe_from: q_tried
        mode: selected
  - id: p_rate
    title: About {{q:q_fav.answer}}
    questions:
    - id: q_grid
      type: grid
      required: true
      title: How would you rate {{q:q_fav.answer}} on…
      rows:
      - id: r_price
        label: Price
      - id: r_content
        label: Content
      options:
      - value: good
        label: Good
      - value: ok
        label: OK
      - value: poor
        label: Poor
  - id: p_why
    questions:
    - id: q_why
      type: textarea
      title: You rated {{q:q_fav.answer}}'s content as **{{q:q_grid.row:r_content.answer}}** and its price
        as **{{q:q_grid.row:r_price.answer}}**. Tell us more.
      body: '_Behind the scenes: your favourite is stored as `{{q:q_fav.value}}`; you''d consider {{q:q_next.answer|nothing
        else}}; you liked {{q:q_tried_best.answer|— (none tried)}} best before._'
  actions:
  - type: disqualify
    reason_code: no_services
    dq_message: Thanks! This study is for people who pay for at least one streaming service.
    when:
      ref:
        kind: question
        id: q_services
      operator: contains
      value: none
  - type: complete
    redirect:
      kind: default
---

# Piping Showcase

_Worked example of every piping form: merge codes (label vs value, multi-select lists, grid cells, URL values, calculated values), fallbacks for answers that may be missing, and option piping (selected / not selected), including the cases that used to go wrong._

tags: example, piping · version 1

> Researcher-readable questionnaire with logic notes. The YAML frontmatter above is the **canonical source** — `survey-agent compile` regenerates the JSON from it. Edit the frontmatter (or the `.survey.json`), then re-render.

## Page `p_intro`

### 1. Thanks for joining from {{url:src|the web}}  `instructions`

<sub>id `q_hello`</sub>

> _Instructions:_ This short study shows how answers are carried forward. Add `&src=email` to the link to see the URL value change.

_Piped: `{{url:src|the web}}`._

### 2. What should we call you? (optional)  `text`

<sub>id `q_name`</sub>

## Page `p_services` — Hi {{q:q_name.answer|there}}

### 3. Which streaming services do you pay for?  `checkbox`  *(required)*

<sub>id `q_services`</sub>

- `netflix` — Netflix

- `prime` — Prime Video

- `disney` — Disney+

- `max` — Max

- `hulu` — Hulu

- `none` — None of these  _(exclusive)_

### 4. Which have you tried in the past but don't pay for now? (optional)  `checkbox`

<sub>id `q_tried`</sub>

- `netflix` — Netflix

- `prime` — Prime Video

- `disney` — Disney+

- `max` — Max

- `hulu` — Hulu

## Page `p_fav` — {{var:n_services}} service(s) — {{q:q_services.answer}}

### 5. Of the ones you pay for, which is your favourite?  `radio`  *(required)*

<sub>id `q_fav`</sub>

_Option piping (selected): only what you picked, and never "None of these"._

- _options piped from Q `q_services` (selected)_

### 6. Which one you don't have would you most consider adding?  `radio`

<sub>id `q_next`</sub>

_Option piping (not selected). Skipped automatically if you picked them all._

- _options piped from Q `q_services` (not_selected)_

### 7. Of the ones you tried before, which did you like best?  `radio`

<sub>id `q_tried_best`</sub>

_Piped from an optional question: skipped, not shown empty, if you left that blank._

- _options piped from Q `q_tried` (selected)_

## Page `p_rate` — About {{q:q_fav.answer}}

### 8. How would you rate {{q:q_fav.answer}} on…  `grid`  *(required)*

<sub>id `q_grid`</sub>

- `good` — Good

- `ok` — OK

- `poor` — Poor

_Grid · radio cells · columns: Good, OK, Poor_

  - row `r_price` — Price

  - row `r_content` — Content

_Piped: `{{q:q_fav.answer}}`._

## Page `p_why`

### 9. You rated {{q:q_fav.answer}}'s content as **{{q:q_grid.row:r_content.answer}}** and its price as **{{q:q_grid.row:r_price.answer}}**. Tell us more.  `textarea`

<sub>id `q_why`</sub>

_Behind the scenes: your favourite is stored as `{{q:q_fav.value}}`; you'd consider {{q:q_next.answer|nothing else}}; you liked {{q:q_tried_best.answer|— (none tried)}} best before._

_Piped: `{{q:q_fav.answer}}`, `{{q:q_fav.value}}`, `{{q:q_grid.row:r_content.answer}}`, `{{q:q_grid.row:r_price.answer}}`, `{{q:q_next.answer|nothing else}}`, `{{q:q_tried_best.answer|— (none tried)}}`._

## Hidden values (computed, never shown to the respondent)

- `n_services` = Q `q_services`

## Logic & terminals

- ⛔ **Screen out** (reason `no_services`) — when Q `q_services` contains none · “Thanks! This study is for people who pay for at least one streaming service.”

- ✅ **Complete** → default
