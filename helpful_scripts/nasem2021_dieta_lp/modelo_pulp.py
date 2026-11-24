"""Modelo de dieta de custo mínimo do NASEM (2021) com PuLP, em forma de funções.

São as mesmas equações e restrições de ``dieta_pulp.py`` (o script didático, que continua igual).
O que muda é a forma de usar:

- ``exigencias(vaca)``: as exigências de uma vaca (bloco 2 do script), com a vaca como parâmetro;
- ``exigencias_media(vacas)``: a média das exigências de várias vacas (uma baia);
- ``carregar_alimentos(...)``: os alimentos do bloco 3, com preços próprios e alimentos bloqueados;
- ``formular(...)``: as rodadas do bloco 4 (modelo, restrições, resolver e medir a dieta);
- ``avaliar(...)``: confere uma dieta fixa contra as exigências, com as mesmas restrições, sem resolver nada.

``formular(exigencias(TABELA_21_1), ...)`` com o consumo da Tabela 21-1 reproduz o resultado do script
(R$ 40,35/dia); o teste ``test_modelo_pulp.py`` confere isso.
"""

from dataclasses import dataclass
from math import exp
from pathlib import Path
import time

import pandas as pd
import pulp

PASTA = Path(__file__).resolve().parent
PREMIX = 0.49            # premix vitamínico e mineral fixo (kg/d)
IMS_TABELA = 29.4        # consumo da Tabela 21-1 (kg MS/d)
MINERAIS = {"magnesium": 0.18, "potassium": 1.00, "sodium": 0.22, "chlorine": 0.32, "sulfur": 0.20}  # % (21-1)
TOLERANCIA = 1e-5        # folga negativa aceita, relativa à escala de cada restrição


@dataclass(frozen=True)
class Vaca:
    pv: float                 # peso corporal (kg)
    pc_mat: float             # peso adulto (kg)
    del_: int                 # dias em lactação
    gestacao: int | None      # dia de gestação; None = vazia
    leite: float              # kg/d
    gordura: float            # % do leite
    proteina: float           # % do leite
    lactose: float            # % do leite
    ganho: float = 0.0        # ganho de peso (kg/d)
    reservas: float = 0.0     # ganho de reservas corporais (kg/d)
    ecc: float = 3.0          # escore corporal (1 a 5)
    multipara: bool = True    # Eq. 2-1: coeficientes de multípara ou primípara


TABELA_21_1 = Vaca(pv=700.0, pc_mat=700.0, del_=100, gestacao=10, leite=55.0, gordura=3.5, proteina=2.83,
                   lactose=4.85, ganho=0.10, reservas=0.21, ecc=3.0, multipara=True)


def exigencias(v):
    """Bloco 2 do script: exigências que não dependem da dieta, para uma vaca."""
    r = v.pv / v.pc_mat
    if v.gestacao is None:
        utero = ganho_utero = ca_gestacao = 0.0
    else:
        k = 0.0243 - 0.0000245 * v.gestacao
        utero = 0.06275 * v.pc_mat * 1.825 * exp(-k * (280 - v.gestacao))   # útero gravídico (kg)
        ganho_utero = k * utero                                              # kg/d
        g1 = v.gestacao - 1
        ca_gestacao = (0.02456 * (exp((0.05581 - 0.00007 * v.gestacao) * v.gestacao)
                                  - exp((0.05581 - 0.00007 * g1) * g1)) * v.pv / 715)
    ell_por_kg_leite = 0.0929 * v.gordura + 0.0547 / 0.93 * v.proteina + 0.0395 * v.lactose

    exig_ell = (0.10 * (v.pv - utero) ** 0.75                                                      # mantença
                + (9.4 * (0.067 + 0.375 * r) + 5.55 * (0.201 - 0.081 * r)) * 0.85 * v.ganho / 0.61  # crescimento
                + ganho_utero * 0.882 / 0.14 * 0.66                                                # gestação
                + ell_por_kg_leite * v.leite                                                       # leite
                + 5.6 * v.reservas)                                                                # reservas
    pm_fixa = ((0.20 * v.pv ** 0.60 * 0.85 + v.proteina * 10 * v.leite
                + 0.86 * 0.85 * v.ganho * (0.201 - 0.081 * r) * 1000) / 0.69
               + 53 * 6.25 * v.pv / 1000 + ganho_utero * 125 / 0.33)
    escala = (v.pc_mat / v.pv) ** 0.22
    ca_fixo = 9.83 * escala * v.ganho + ca_gestacao + (0.295 + 0.239 * v.proteina) * v.leite
    p_fixo = 0.0006 * v.pv + (1.2 + 4.635 * escala) * v.ganho + (0.49 + 0.13 * v.proteina) * v.leite
    par = 1.0 if v.multipara else 0.0                                                             # Eq. 2-1
    ims_eq = (((3.7 + par * 5.7) + 0.305 * ell_por_kg_leite * v.leite + 0.022 * v.pv
               + (-0.689 - par * 1.87) * v.ecc) * (1 - (0.212 + par * 0.136) * exp(-0.053 * v.del_)))
    return dict(EXIG_ELL=exig_ell, PM_FIXA=pm_fixa, CA_FIXO=ca_fixo, P_FIXO=p_fixo, IMS_EQ=ims_eq,
                PB_LEITE=v.leite * v.proteina / 100 / 0.93, PV=v.pv)


def exigencias_media(vacas):
    """Média das exigências de várias vacas: a baia é atendida na média, como no RuFaS."""
    todas = [exigencias(v) for v in vacas]
    return {k: sum(e[k] for e in todas) / len(todas) for k in todas[0]}


def carregar_alimentos(precos_custom=None, bloqueados=()):
    """Bloco 3 do script. Preços próprios em R$/kg natural, pelo código RuFaS da tabela de preços."""
    precos = pd.read_csv(PASTA.parent / "nrc2001_dieta_lp" / "alimentos_precos.csv")
    precos["preco_natural"] = precos.preco_cotado / precos.kg_por_unidade
    preco_premix = precos[precos.fixo_kg.notna()].preco_natural.iloc[0]
    precos = precos[precos.fixo_kg.isna()].copy()
    precos["id"] = precos.rufas_id.astype(int)
    for ident, preco in (precos_custom or {}).items():
        precos.loc[precos.id == int(ident), "preco_natural"] = float(preco)
    precos = precos[~precos.id.isin([int(i) for i in bloqueados])]
    precos["id_biblioteca"] = precos.id.replace({19: 18})   # Tifton 85 -> "Bermudagrass hay" (não há Tifton no NASEM)

    lib = pd.read_csv(PASTA.parents[1] / "input" / "data" / "feed" / "NASEM_Comp_with_TDN.csv").set_index("rufas_id")
    F = lib.loc[precos.id_biblioteca].reset_index(drop=True).fillna(0)
    F.index = precos.alimento.values
    F["id"] = precos.id.values
    F["id_biblioteca"] = precos.id_biblioteca.values
    F["preco"] = precos.preco_natural.values / (F.DM.values / 100)   # R$ por kg de MS
    if "Fosfato bicálcico" in F.index:
        F.loc["Fosfato bicálcico", ["Abs_calcium", "Abs_phosphorus"]] = [0.60, 0.75]   # Tabela 19-3
    if "Sabões de cálcio" in F.index:
        F.loc["Sabões de cálcio", "calcium"] = 12.0                                   # a Tabela 19-1 não traz o Ca

    F["forragem"] = (F.feed_type == "Forage").astype(int)
    F["umida"] = F.forragem * (F.DM < 50)                                         # silagens
    F["fdn_forragem"] = F.NDF * F.forragem
    F["gordura_sup"] = F.EE * F.index.isin(["Caroço de algodão", "Soja grão", "Sabões de cálcio"])
    F["pdr"] = F.CP / 100 * (1 - F.RUP / 100)                                     # kg de PDR por kg de MS
    F["pndr_dig"] = F.CP / 100 * F.RUP / 100 * F.dRUP / 100                       # kg de PNDR digestível por kg
    tem_fdn = (F.NDF > 0) & (F.lignin > 0)
    F["dfdn_base"] = (0.75 * (F.NDF - F.lignin) * (1 - (F.lignin / F.NDF) ** 0.667) / F.NDF).where(tem_fdn, 0)
    F["ed_verdadeira"] = (F.DE_Base + 0.318).where(F.DE_Base > 0, 0)              # ED sem o desconto endógeno
    return F, preco_premix


def _coeficientes(F, req, consumo, amido_pct, g, pb_mic):
    """Energia (ELL) de 1 kg de cada alimento e proteína microbiana para a dieta suposta (Eq. 3-3 a 3-12, 6-3)."""
    aj_fdn = -0.0059 * (amido_pct - 26) - 1.1 * (consumo / req["PV"] - 0.035)       # Eq. 3-5a
    aj_amido = -1.0 * (consumo / req["PV"] - 0.035)                                  # Eq. 3-5b
    pbmf = (11.62 + 13.4 * F.NDF / 100) / 1000                                       # PB metabólica fecal (kg/kg)
    ed = F.ed_verdadeira + 0.042 * F.NDF * aj_fdn + 0.0423 * F.starch * aj_amido
    metano = 0.294 - 0.347 * F.FA / consumo + 0.0409 * F.NDF * (F.dfdn_base + aj_fdn) / consumo   # Eq. 3-9
    urina = 0.0146 * 160 * (F.pdr + F.pndr_dig - pbmf)                               # Eq. 3-10
    ell = 0.66 * (ed - (5.65 * pbmf + 0.0040 * 34.3) - metano - urina)               # Eq. 3-12
    c_ell = 0.66 * (-PREMIX * (5.65 * 0.01162 + 0.0040 * 34.3 + 0.294 - 0.0146 * 160 * 0.01162)
                    - 0.00565 * 0.2 * pb_mic + 0.0146 * 160 * (0.2 * pb_mic / 1000 + req["PB_LEITE"]))
    return dict(ell=ell, c_ell=c_ell,
                pm_mic_base=0.824 * 0.80 * 6.25 * 101 * g,      # PM microbiana fixa (Eq. 6-3)
                pm_por_kg_pdr=0.824 * 0.80 * 6.25 * 82.6 * g)   # PM microbiana por kg de PDR útil


def _restricoes(F, x, pdr_util, T, req, c, ims, preco_premix, orcamento):
    """As restrições do bloco 4 do script, com os mesmos nomes. Devolve {nome: LpConstraint}."""
    A = list(F.index)

    def soma(col):
        return pulp.lpSum(F[col][i] * x[i] for i in A)

    R = {
        # Grupo 1: consumo
        "consumo_min": T >= ims[0],
        "consumo_max": T <= ims[1],
        # Grupo 2: energia
        "energia": pulp.lpSum(c["ell"][i] * x[i] for i in A) + c["c_ell"] >= req["EXIG_ELL"],
        # Grupo 3: proteína
        "pm": (c["pm_mic_base"] + c["pm_por_kg_pdr"] * pdr_util + 1000 * soma("pndr_dig")
               >= req["PM_FIXA"] + 0.73 / 0.69 * (11.62 * T + pulp.lpSum(0.134 * F.NDF[i] * x[i] for i in A))),
        "pdr_util_ate_pdr": pdr_util <= soma("pdr"),
        "pdr_util_ate_12": pdr_util <= 0.12 * T,
        "pdr_min": 100 * soma("pdr") >= 10 * T,
        "pb_max": soma("CP") <= 19 * T,
        # Grupo 4: fibra e amido (Tabela 5-1)
        "fdn_forragem_min": soma("fdn_forragem") >= 15 * T,
        "fdn_min": soma("NDF") >= 25 * T,
        "amido_max": soma("starch") <= 30 * T,
        "fdn_mais_2fdnf": pulp.lpSum((F.NDF[i] + 2 * F.fdn_forragem[i]) * x[i] for i in A) >= 63 * T,
        "amido_menos_2fdnf": pulp.lpSum((F.starch[i] - 2 * F.fdn_forragem[i]) * x[i] for i in A) <= -8 * T,
        # Grupo 5: gordura
        "ee_max": soma("EE") <= 7 * T,
        "gordura_sup_max": soma("gordura_sup") <= 4 * T,
        # Grupo 6: minerais
        "ca_absorvivel": (pulp.lpSum(F.calcium[i] * 10 * F.Abs_calcium[i] * x[i] for i in A)
                          >= req["CA_FIXO"] + 0.90 * T),
        "p_absorvivel": (pulp.lpSum(F.phosphorus[i] * 10 * F.Abs_phosphorus[i] * x[i] for i in A)
                         >= req["P_FIXO"] + 1.0 * T),
        "ca_max": soma("calcium") <= 1.0 * T,
        "p_max": soma("phosphorus") <= 0.42 * T,
        "ca_maior_p": pulp.lpSum((F.calcium[i] - F.phosphorus[i]) * x[i] for i in A) >= 0,
        "ca_ate_7p": pulp.lpSum((7 * F.phosphorus[i] - F.calcium[i]) * x[i] for i in A) >= 0,
    }
    for mineral, minimo in MINERAIS.items():
        R[f"{mineral}_min"] = soma(mineral) >= minimo * T
    if orcamento is not None:   # teto de custo opcional (não existe no script)
        R["teto_custo"] = pulp.lpSum(F.preco[i] * x[i] for i in A) + PREMIX * preco_premix <= orcamento
    return R


# Rótulo, unidade e se a folga é dividida pelo consumo (restrições em % da MS).
ROTULOS = {
    "consumo_min": ("Consumo mínimo", "kg MS/dia", False), "consumo_max": ("Consumo máximo", "kg MS/dia", False),
    "energia": ("Energia líquida de lactação (ELL)", "Mcal/dia", False),
    "pm": ("Proteína metabolizável", "g/dia", False),
    "pdr_util_ate_pdr": ("PDR útil até a PDR da dieta", "kg/dia", False),
    "pdr_util_ate_12": ("PDR útil até 12% da MS", "kg/dia", False),
    "pdr_min": ("PDR mínima 10%", "% MS", True), "pb_max": ("PB máxima 19%", "% MS", True),
    "fdn_forragem_min": ("FDN de forragem mínima 15%", "% MS", True), "fdn_min": ("FDN mínima 25%", "% MS", True),
    "amido_max": ("Amido máximo 30%", "% MS", True), "fdn_mais_2fdnf": ("FDN + 2·FDNf ≥ 63", "% MS", True),
    "amido_menos_2fdnf": ("Amido − 2·FDNf ≤ −8", "% MS", True), "ee_max": ("Extrato etéreo máximo 7%", "% MS", True),
    "gordura_sup_max": ("Gordura suplementar máxima 4%", "% MS", True),
    "ca_absorvivel": ("Cálcio absorvível", "g/dia", False), "p_absorvivel": ("Fósforo absorvível", "g/dia", False),
    "ca_max": ("Ca máximo 1%", "% MS", True), "p_max": ("P máximo 0,42%", "% MS", True),
    "ca_maior_p": ("Ca maior ou igual a P", "% MS", True), "ca_ate_7p": ("Ca até 7 vezes o P", "% MS", True),
    "magnesium_min": ("Magnésio mínimo 0,18%", "% MS", True), "potassium_min": ("Potássio mínimo 1%", "% MS", True),
    "sodium_min": ("Sódio mínimo 0,22%", "% MS", True), "chlorine_min": ("Cloro mínimo 0,32%", "% MS", True),
    "sulfur_min": ("Enxofre mínimo 0,20%", "% MS", True), "teto_custo": ("Teto de custo", "R$/dia", False),
}
ESCALAS = {"kg MS/dia": 10., "Mcal/dia": 10., "g/dia": 100., "kg/dia": 1., "% MS": 1., "R$/dia": 10.}


def _folgas(R, total):
    """Folga de cada restrição na unidade do rótulo; positiva ou zero = atendida."""
    out = []
    for nome, c in R.items():
        valor = c.value()
        folga = valor if c.sense == pulp.LpConstraintGE else -valor
        rotulo, unidade, por_consumo = ROTULOS[nome]
        if por_consumo:
            folga /= total
        escala = ESCALAS[unidade]
        out.append(dict(nome=rotulo, codigo=nome, folga=float(folga), escala=escala, unidade=unidade,
                        atendida=bool(folga >= -TOLERANCIA * escala)))
    return out


def _medir(F, kg):
    """Mede a dieta escolhida (Eq. 6-3 e 6-4): consumo, % de amido, eficiência g e PB microbiana."""
    total = kg.sum() + PREMIX
    pct = {c: (kg * F[c]).sum() / total for c in ("NDF", "ADF", "starch", "CP", "fdn_forragem", "umida")}
    pct["umida"] *= 100
    dig_fdn = (-31.9 + 0.721 * pct["NDF"] - 0.247 * pct["starch"] + 6.63 * pct["CP"] - 0.211 * pct["CP"] ** 2
               - 38.7 * pct["ADF"] / pct["NDF"] - 0.121 * pct["umida"] + 1.51 * total)
    dig_amido = (71.2 - 1.45 * total + 0.424 * pct["fdn_forragem"] + 1.39 * pct["starch"]
                 - 0.0219 * pct["starch"] ** 2 - 0.154 * pct["umida"])
    fdn_rumen = dig_fdn * pct["NDF"] / 100 * total / 100        # kg de FDN degradada no rúmen
    amido_rumen = dig_amido * pct["starch"] / 100 * total / 100  # kg de amido degradado no rúmen
    g = 1 / ((1 + 0.094 / fdn_rumen) * (1 + 0.027 / amido_rumen))
    pb_mic = 6.25 * g * (101 + 82.6 * min((kg * F.pdr).sum(), 0.12 * total))
    return total, pct, (total, pct["starch"], g, pb_mic)


def _variaveis(F, tetos):
    x = {i: pulp.LpVariable(f"x_{n}", lowBound=0, upBound=tetos.get(int(F.id_biblioteca[i])))
         for n, i in enumerate(F.index)}
    return x, pulp.LpVariable("pdr_util", lowBound=0)


def formular(req, F, preco_premix, ims, tetos=None, orcamento=None, max_rodadas=100):
    """Bloco 4 do script: rodadas de LP até a dieta suposta e a medida coincidirem.

    req: exigências (``exigencias`` ou ``exigencias_media``); ims: (consumo mínimo, máximo) em kg MS;
    tetos: {código RuFaS na biblioteca: kg MS máximo}; orcamento: R$/vaca/dia máximo.
    """
    inicio = time.monotonic()
    tetos = tetos or {}
    A = list(F.index)
    consumo, amido_pct, g, pb_mic = IMS_TABELA, 26.0, 0.97, 2100.0   # chute inicial do script
    status = "Not Solved"
    for rodada in range(1, max_rodadas + 1):
        c = _coeficientes(F, req, consumo, amido_pct, g, pb_mic)
        model = pulp.LpProblem("Dieta_NASEM", pulp.LpMinimize)
        x, pdr_util = _variaveis(F, tetos)
        T = pulp.lpSum(x[i] for i in A) + PREMIX
        model.setObjective(pulp.lpSum(F.preco[i] * x[i] for i in A) + PREMIX * preco_premix)
        R = _restricoes(F, x, pdr_util, T, req, c, ims, preco_premix, orcamento)
        for nome, restricao in R.items():
            model.addConstraint(restricao, name=nome)
        model.solve(pulp.PULP_CBC_CMD(msg=False))
        status = pulp.LpStatus[model.status]
        if status != "Optimal":
            return dict(sucesso=False, status=status, rodadas=rodada,
                        mensagem=f"O modelo PuLP não encontrou dieta viável (status {status}).",
                        tempo_segundos=time.monotonic() - inicio)
        kg = pd.Series({i: x[i].value() or 0.0 for i in A})
        total, pct, novo = _medir(F, kg)
        if max(abs(a - b) / max(abs(b), 1.0) for a, b in zip(novo, (consumo, amido_pct, g, pb_mic))) < 1e-8:
            break
        consumo, amido_pct, g, pb_mic = novo
    else:
        return dict(sucesso=False, status="sem_convergencia", rodadas=max_rodadas,
                    mensagem="As rodadas do modelo PuLP não convergiram.", tempo_segundos=time.monotonic() - inicio)
    custo = pulp.value(model.objective)
    ingredientes = [dict(id=int(F.id_biblioteca[i]), nome=i, kg_ms=float(kg[i]),
                         kg_natural=float(kg[i] / (F.DM[i] / 100)), custo_dia=float(kg[i] * F.preco[i]))
                    for i in A if kg[i] > 1e-7]
    ingredientes.append(dict(id=999, nome="Premix (fixo)", kg_ms=PREMIX, kg_natural=PREMIX,
                             custo_dia=PREMIX * preco_premix))
    return dict(sucesso=True, status=status, rodadas=rodada, custo_total_dia=float(custo),
                consumo_total_ms=float(total),
                kg=kg, ingredientes=ingredientes, restricoes=_folgas(R, total),
                composicao={k: float(v) for k, v in pct.items()}, exigencias={k: float(v) for k, v in req.items()},
                tempo_segundos=time.monotonic() - inicio)


def avaliar(req, F, kg, ims, preco_premix, orcamento=None):
    """Confere uma dieta fixa (kg MS por alimento) contra as exigências, com as restrições do modelo.

    Não resolve nada: a energia e a proteína microbiana são calculadas para a própria dieta (bloco
    "mede a dieta" do script) e cada restrição é avaliada com esses valores.
    """
    kg = pd.Series(kg).reindex(F.index).fillna(0.0)
    total, pct, (consumo, amido_pct, g, pb_mic) = _medir(F, kg)
    c = _coeficientes(F, req, consumo, amido_pct, g, pb_mic)
    x, pdr_util = _variaveis(F, {})
    for i in F.index:
        x[i].varValue = float(kg[i])
    pdr_util.varValue = float(min((kg * F.pdr).sum(), 0.12 * total))   # o melhor valor possível da PDR útil
    T = pulp.lpSum(x[i] for i in F.index) + PREMIX
    return _folgas(_restricoes(F, x, pdr_util, T, req, c, ims, preco_premix, orcamento), total)
