%% careeros.cls — resume class derived from the user's hand-tuned base_resume.tex
%%
%% Design rule: the VISUAL output must match data/resumes/base_resume.tex
%% (Helvetica sans, a4paper, 0.6in side margins, uppercase bold section
%% headings with a full-width rule). What changed from that file is only the
%% *mechanism*: every two-column row (title left / meta right) is built from
%% two fixed-width minipages instead of a bare \hfill, and bullets are a real
%% itemize instead of literal "- " prefixes.
%%
%% Why that matters: a bare "\textbf{Long Title} \hfill \textbf{[long stack]}"
%% silently overflows the right margin as soon as the two strings exceed
%% \textwidth, and a "- " bullet that wraps puts its second line hard against
%% the left margin instead of hanging under the text. Both are invisible in a
%% one-off hand-written resume and unavoidable in a generated one, where the
%% strings change with every JD. The minipages wrap instead of overflowing and
%% the itemize gives a proper hanging indent, so alignment holds for any input.

\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{careeros}[2026/09/29 CareerOS resume class]

\LoadClass[a4paper,10pt]{article}

% ── Page geometry (matches base_resume.tex) ───────────────────────────────────
\RequirePackage[left=0.6in, right=0.6in, top=0.5in, bottom=0.6in]{geometry}

% ── Font: Helvetica clone as the default family ───────────────────────────────
\RequirePackage[T1]{fontenc}
\RequirePackage[utf8]{inputenc}
\RequirePackage{helvet}
\renewcommand{\familydefault}{\sfdefault}
\RequirePackage{microtype}

% ── Links ─────────────────────────────────────────────────────────────────────
\RequirePackage{xcolor}
\RequirePackage{hyperref}
\hypersetup{colorlinks=true, linkcolor=black, urlcolor=blue, pdfborder={0 0 0}}
\RequirePackage{url}
\urlstyle{same}

% ── Layout helpers ────────────────────────────────────────────────────────────
\RequirePackage{enumitem}
\RequirePackage{titlesec}
\RequirePackage{array}

\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\setlength{\tabcolsep}{0pt}

% Prefer a slightly loose line over an overfull box poking into the margin.
% Every overfull hbox is a visible alignment defect, so we trade tight spacing
% for a guaranteed-clean right edge; the compiler still reports any box that
% survives this, and the build treats that report as a failure signal.
\tolerance=2000
\emergencystretch=2em
\hbadness=10000

% ── Section headings (matches base_resume.tex) ────────────────────────────────
\titleformat{\section}{\large\bfseries\uppercase}{}{0em}{}[\titlerule]
\titlespacing*{\section}{0pt}{0.85em}{0.35em}

% ── Two-column row: content left, meta right, both wrap ───────────────────────
% #1 left block, #2 right block. Widths sum to 0.98 so \hfill always has slack.
\newcommand{\tworow}[2]{%
  \noindent%
  \begin{minipage}[t]{0.615\textwidth}\raggedright #1\end{minipage}%
  \hfill%
  \begin{minipage}[t]{0.365\textwidth}\raggedleft #2\end{minipage}%
  \par%
}

% ── Header ────────────────────────────────────────────────────────────────────
% \resumeheader{Name}{contact line}{links line}
\newcommand{\resumeheader}[3]{%
  \begin{center}
    {\LARGE \textbf{#1}} \\[0.5em]
    \small #2 \\[0.3em]
    \small #3
  \end{center}
}

% ── Experience entry ──────────────────────────────────────────────────────────
% \expentry{Role}{Company}{Dates | Location} — empty #3 suppresses the line,
% so an entry with no dates recorded does not leave a blank row behind.
\newcommand{\expentry}[3]{%
  \tworow{\textbf{#1}}{#2}%
  \ifx\relax#3\relax\else\textit{\small #3}\par\fi
}

% ── Project entry ─────────────────────────────────────────────────────────────
% \projentry{Title}{Tech stack}{link text}{link url}
% An empty #3 suppresses the URL line entirely.
\newcommand{\projentry}[4]{%
  \tworow{\textbf{#1}}{\textbf{\small[ #2 ]}}%
  \ifx\relax#3\relax\else\vspace{-0.15em}\textit{\small\href{#4}{#3}}\par\fi
}

% ── Bullet list ───────────────────────────────────────────────────────────────
\newenvironment{rbullets}{%
  \vspace{0.12em}%
  \begin{itemize}[leftmargin=1.1em, labelsep=0.4em, label=\textbullet,
                  itemsep=0.12em, topsep=0.08em, parsep=0pt, partopsep=0pt]%
}{%
  \end{itemize}%
}

% Vertical gap between two entries in the same section.
\newcommand{\entrygap}{\vspace{0.55em}}

% ── Skills row ────────────────────────────────────────────────────────────────
% \skillrow{Category}{comma-separated list} — wrapped list hangs under itself
% rather than falling back to the left margin.
\newcommand{\skillrow}[2]{%
  \noindent%
  \begin{minipage}[t]{0.27\textwidth}\raggedright\textbf{#1}\end{minipage}%
  \begin{minipage}[t]{0.73\textwidth}\raggedright #2\end{minipage}%
  \par\vspace{0.18em}%
}

% ── Certification / award line ────────────────────────────────────────────────
% \certentry{description}{link text}{link url} — empty #2 suppresses the link.
% Wider left column than \tworow: the description is a full title plus a year,
% while the right column only ever holds a short shortened-URL label.
\newcommand{\certentry}[3]{%
  \noindent%
  \begin{minipage}[t]{0.80\textwidth}\raggedright #1\end{minipage}%
  \hfill%
  \begin{minipage}[t]{0.18\textwidth}\raggedleft
    \ifx\relax#2\relax\else\small\href{#3}{#2}\fi
  \end{minipage}%
  \par\vspace{0.2em}%
}
