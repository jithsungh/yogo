\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{careeros}[2024/01/01 CareerOS resume class]

\LoadClass[10pt,letterpaper]{article}

% ── Page geometry ─────────────────────────────────────────────────────────────
\usepackage[
  top=0.45in, bottom=0.45in,
  left=0.55in, right=0.55in
]{geometry}

% ── Font ──────────────────────────────────────────────────────────────────────
\usepackage[T1]{fontenc}
\usepackage{lmodern}          % swap for FiraSans/SourceSansPro if preferred
\usepackage{microtype}        % better text wrapping

% ── Encoding + URL handling ───────────────────────────────────────────────────
\usepackage[utf8]{inputenc}
\usepackage[hidelinks]{hyperref}
\usepackage{url}
\urlstyle{same}

% ── Layout helpers ────────────────────────────────────────────────────────────
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage{multicol}
\usepackage{setspace}

\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\setlength{\tabcolsep}{0pt}

% ── Section headings ──────────────────────────────────────────────────────────
% Single rule underline, all-caps, compact spacing - ATS-friendly (no boxes/colors)
\titleformat{\section}{
  \vspace{-6pt}\scshape\raggedright\large
}{}{0em}{}[\titlerule\vspace{-4pt}]

% ── Custom commands ───────────────────────────────────────────────────────────

% \experienceheading{Company}{Role}{Date range}{Location}
\newcommand{\experienceheading}[4]{
  \vspace{2pt}
  \begin{tabular*}{\textwidth}{l@{\extracolsep{\fill}}r}
    \textbf{#1} $|$ \textit{\small #2} & \small #3 \\
    \multicolumn{2}{l}{\small\textit{#4}}
  \end{tabular*}
  \vspace{-2pt}
}

% \projectheading{Title}{Tech stack}{URL (optional)}
\newcommand{\projectheading}[3]{
  \vspace{2pt}
  \begin{tabular*}{\textwidth}{l@{\extracolsep{\fill}}r}
    \textbf{#1} $|$ \textit{\small #2} & \small \href{#3}{#3}
  \end{tabular*}
  \vspace{-2pt}
}

% Bullet list with tight spacing - ATS parsers read these reliably
\newenvironment{bulletlist}{
  \begin{itemize}[leftmargin=0.15in, label=\textbullet, itemsep=-1pt, topsep=2pt]
}{
  \end{itemize}
}
