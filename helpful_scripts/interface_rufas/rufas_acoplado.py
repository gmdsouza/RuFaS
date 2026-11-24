"""Otimizador da interface acoplado à simulação completa do RuFaS.

Fluxo
-----
1. O RuFaS simula o rebanho de exemplo dia a dia (modo ``animals_only``).
2. No dia 1 e a cada ``intervalo_dieta`` dias, no ponto em que o próprio RuFaS
   reformula as rações, a baia de vacas em lactação recebe a dieta de custo
   mínimo de um dos dois otimizadores, calculada para as vacas que estão na baia
   naquele dia (leite, DEL, gestação e peso gerados pela simulação):
   - ``pulp``: o modelo NASEM próprio, escrito com PuLP
     (``helpful_scripts/nasem2021_dieta_lp/modelo_pulp.py``, mesmas equações de
     ``dieta_pulp.py``), com a média das exigências das vacas da baia;
   - ``slsqp``: o otimizador da interface (``rufas_solver.Formulator``), com as
     exigências nativas do RuFaS e as restrições do ``RationOptimizer``.
3. A simulação segue com essa dieta até a próxima reformulação. Todos os dias, a
   dieta em uso é conferida contra as exigências das vacas daquele dia, com as
   restrições do próprio otimizador. As outras baias (bezerras, novilhas e
   pré-parto) seguem o fluxo nativo do RuFaS.

O leite é resultado da simulação (curva de lactação do RuFaS), não uma meta
digitada. Nenhum arquivo do núcleo ``RUFAS/`` nem de ``input/`` é alterado: as
entradas da rodada são cópias gravadas em ``output/acoplado/<rodada>/``.
"""
import argparse
import contextlib
import csv
import datetime
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path

try:
    from . import rufas_solver as solver
except ImportError:
    import rufas_solver as solver

# rufas_solver coloca a raiz do clone no sys.path; só depois o pacote RUFAS pode ser importado.
from RUFAS.biophysical.animal.animal_module_constants import AnimalModuleConstants  # noqa: E402
from RUFAS.biophysical.animal.data_types.animal_combination import AnimalCombination  # noqa: E402
from RUFAS.biophysical.animal.data_types.growth import GrowthInputs  # noqa: E402
from RUFAS.biophysical.animal.data_types.nutrition_data_structures import NutritionEvaluationResults  # noqa: E402
from RUFAS.biophysical.animal.herd_manager import HerdManager  # noqa: E402
from RUFAS.biophysical.animal.milk.milk_production import MilkProduction  # noqa: E402
from RUFAS.biophysical.animal.nutrients.nutrition_evaluator import NutritionEvaluator  # noqa: E402
from RUFAS.data_structures.feed_storage_to_animal_connection import NutrientStandard  # noqa: E402

ROOT = solver.ROOT
sys.path.insert(0, str(ROOT / 'helpful_scripts' / 'nasem2021_dieta_lp'))
import modelo_pulp  # noqa: E402

OTIMIZADORES = {'pulp': 'PuLP/CBC · modelo NASEM próprio (modelo_pulp)',
                'slsqp': 'SciPy/SLSQP · restrições nativas do RuFaS'}

MODEL = 'rufas-acoplado-simulacao-v1'
METADATA = 'input/metadata/example_animals_only_metadata.json'
CONFIG = 'input/data/config/example_animals_only_config.json'
ANIMAL = 'input/data/animal/example_freestall_animal.json'
FEED = 'input/data/feed/example_Midwest_feed.json'
TASK = 'input/data/tasks/example_animals_only_task.json'
START = datetime.date(2013, 1, 1)  # o clima de exemplo do RuFaS cobre 2013–2019
MAX_DAYS = 730
# As rações de exemplo das outras baias usam duas misturas de fazenda que só
# existem na biblioteca NASEM. No NRC, a cópia de entrada da rodada troca cada
# uma por duas partes iguais de alimentos da biblioteca NRC.
NRC_SUBSTITUTES = {301: (234, 231),   # mistura mineral -> calcário calcítico + fosfato bicálcico
                   302: (167, 163)}   # mistura de subprodutos (26% PB) -> farelo de soja + casca de soja


def validar(scenario):
    """Valida o cenário do modo acoplado. O rebanho e o leite vêm do RuFaS."""
    if not isinstance(scenario, dict):
        raise ValueError('Cenário deve ser um objeto.')
    defaults = dict(padrao='NASEM', dias_simulacao=90, intervalo_dieta=30, preco_leite_kg=2.75,
                    teto_caroco=2.53, orcamento_dia=None, precos_custom={}, alimentos_bloqueados=[],
                    premix_composicao={}, semente=42, conferencia_diaria=True, otimizador=None,
                    faixa_consumo=AnimalModuleConstants.DMI_CONSTRAINT_FRACTION)
    unknown = set(scenario) - set(defaults)
    if unknown:
        raise ValueError('Parâmetros desconhecidos no modo acoplado: ' + ', '.join(sorted(unknown)))
    s = {**defaults, **scenario}
    if s['padrao'] not in ('NRC', 'NASEM'):
        raise ValueError('Use NRC ou NASEM.')
    for key, (lo, hi) in dict(dias_simulacao=(1, MAX_DAYS), intervalo_dieta=(1, 60), semente=(0, 2**31 - 1)).items():
        v = s[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or not lo <= v <= hi:
            raise ValueError(f'{key}: use um inteiro entre {lo} e {hi}.')
        s[key] = int(v)
    if s['intervalo_dieta'] > s['dias_simulacao']:
        raise ValueError('O intervalo de reformulação não pode passar do número de dias simulados.')
    s['preco_leite_kg'] = float(s['preco_leite_kg'])
    if not math.isfinite(s['preco_leite_kg']) or not 0 <= s['preco_leite_kg'] <= 30:
        raise ValueError('preco_leite_kg: fora do domínio 0–30.')
    s['conferencia_diaria'] = bool(s['conferencia_diaria'])
    return _validar_otimizador(s)


def _validar_otimizador(s):
    if s['otimizador'] is None:
        s['otimizador'] = 'pulp' if s['padrao'] == 'NASEM' else 'slsqp'
    if s['otimizador'] not in OTIMIZADORES:
        raise ValueError('otimizador: use "pulp" ou "slsqp".')
    if s['otimizador'] == 'pulp' and s['padrao'] != 'NASEM':
        raise ValueError('O modelo PuLP usa as equações do NASEM; para o NRC, use o otimizador SLSQP.')
    s['faixa_consumo'] = float(s['faixa_consumo'])
    if not 0 < s['faixa_consumo'] < 1:
        raise ValueError('faixa_consumo: use uma fração entre 0 e 1 (0,30 = ±30% do consumo previsto).')
    return s


def _cenario_baia(s, cows, temperature):
    """Cenário do Formulator com as médias da baia (peso, leite, composição)."""
    n = len(cows)

    def mean(key):
        return sum(c[key] for c in cows) / n

    def clamp(v, lo, hi):
        return min(hi, max(lo, v))

    return dict(padrao=s['padrao'], peso_vivo=clamp(mean('peso'), 300, 1000),
                peso_adulto=clamp(mean('peso_adulto'), 300, 1100), leite_kg=clamp(mean('leite'), 5, 80),
                gordura_pct=MilkProduction.fat_percent, proteina_pct=MilkProduction.true_protein_percent,
                lactose_pct=MilkProduction.lactose_percent,
                del_dias=int(clamp(round(mean('del_ref')), 1, 304)),
                paridade=int(clamp(round(mean('paridade')), 1, 10)),
                dias_gestacao=0, idade_dias=int(clamp(round(mean('idade')), 500, 6000)),
                intervalo_partos=int(clamp(round(mean('intervalo_partos')), 280, 800)),
                escore_corporal=clamp(mean('escore'), 1, 5), alojamento='Barn',
                distancia=clamp(mean('distancia'), 0, 10000), dias_simulacao=1, intervalo_dieta=1,
                vacas_lactacao=n, tamanho_rebanho=n, temperatura=clamp(temperature, -10, 40),
                preco_leite_kg=s['preco_leite_kg'], teto_caroco=s['teto_caroco'], orcamento_dia=s['orcamento_dia'],
                precos_custom=s['precos_custom'], alimentos_bloqueados=s['alimentos_bloqueados'],
                premix_composicao=s['premix_composicao'])


def _crescimento(animal):
    """Ganho de peso-alvo e variação de reservas do dia, pelas fórmulas de crescimento do próprio RuFaS.

    O ganho diário que o RuFaS guarda soma o ganho-alvo, o crescimento do feto e a variação de
    reservas; o modelo PuLP trata o feto à parte. No dia 1 esse valor ainda é zero. Por isso as
    duas parcelas são calculadas aqui, com as mesmas entradas que a vaca usa na rotina diária.
    """
    inputs = GrowthInputs(days_in_pregnancy=animal.days_in_pregnancy, animal_type=animal.animal_type,
                          body_weight=animal.body_weight, mature_body_weight=animal.mature_body_weight,
                          birth_weight=animal.birth_weight, days_born=animal.days_born,
                          days_in_milk=animal.days_in_milk, conceptus_weight=animal.conceptus_weight,
                          gestation_length=animal.gestation_length, calf_birth_weight=animal.calf_birth_weight,
                          calves=animal.calves, calving_interval=animal.calving_interval)
    growth = animal.growth
    target = growth._calculate_cow_target_daily_growth(inputs)
    tissue, _ = growth._calculate_cow_body_weight_tissue_change(inputs)
    return float(target), float(tissue)


def _vacas(pen):
    """Estado de cada vaca da baia, como a própria simulação o usa nas exigências."""
    cows = []
    for animal in pen.animals_in_pen.values():
        milking = animal.is_milking
        milk = animal.milk_production.daily_milk_produced
        if milking and milk <= 0:
            # No dia 1 a simulação ainda não calculou o leite: usa a curva de Wood
            # da própria vaca, descontado o corte de leite em vigor.
            mp = animal.milk_production
            milk = max(0., MilkProduction.calculate_daily_milk_production(
                max(animal.days_in_milk, 1), mp.wood_l, mp.wood_m, mp.wood_n) - mp.milk_production_reduction)
        target, tissue = _crescimento(animal)
        cows.append(dict(
            ganho_alvo=target, reservas=tissue, id=animal.id, peso=animal.body_weight,
            peso_adulto=animal.mature_body_weight,
            gest=animal.days_in_pregnancy if animal.is_pregnant else None,
            del_=animal.days_in_milk if milking else None, del_ref=animal.days_in_milk if milking else 0,
            escore=animal.body_condition_score_5, ganho=animal.growth.daily_growth,
            tecido=animal.growth.daily_tissue_change, tipo=animal.animal_type, paridade=animal.calves,
            intervalo_partos=animal.calving_interval, leite=milk,
            lactante=milking, idade=animal.days_born, distancia=animal.daily_distance,
            fosforo=animal.nutrients.phosphorus_requirement))
    return cows


class PenFormulator(solver.Formulator):
    """Formulator da interface com as exigências médias das vacas reais da baia.

    A média é feita como no RuFaS (soma das exigências de cada vaca dividida pelo
    número de vacas), mas recalculada para a FDN/energia da dieta em teste, como
    o Formulator já faz para a vaca representativa da interface.
    """

    def __init__(self, scenario, cows, temperature, housing):
        self.cows = cows
        self.temperature = temperature
        self.housing = housing
        self.req_cache = {}
        super().__init__(scenario)

    def requirements(self, day, ndf=30., energy=1.6, tdn=.65):
        key = (day, ndf, energy, tdn)
        if key in self.req_cache:
            return self.req_cache[key]
        total = None
        for c in self.cows:
            params = dict(body_weight=c['peso'], mature_body_weight=c['peso_adulto'],
                          day_of_pregnancy=None if c['gest'] is None else c['gest'] + day,
                          body_condition_score_5=c['escore'],
                          days_in_milk=None if c['del_'] is None else c['del_'] + day,
                          average_daily_gain_heifer=c['ganho'], animal_type=c['tipo'], parity=c['paridade'],
                          calving_interval=c['intervalo_partos'], milk_fat=MilkProduction.fat_percent,
                          milk_true_protein=MilkProduction.true_protein_percent,
                          milk_lactose=MilkProduction.lactose_percent, milk_production=c['leite'],
                          housing=self.housing, distance=c['distancia'],
                          process_based_phosphorus_requirement=c['fosforo'])
            if self.standard is NutrientStandard.NASEM:
                r = solver.NASEMRequirementsAdapter.calculate_requirements(
                    **params, lactating=c['lactante'], ndf_percentage=ndf, tissue_weight_change=c['tecido'])
            else:
                r = solver.NRCRequirementsCalculator.calculate_requirements(
                    **params, previous_temperature=self.temperature, net_energy_diet_concentration=energy,
                    days_born=c['idade'] + day, TDN_fraction=tdn)
            total = r if total is None else total + r
        value = total / len(self.cows)
        if len(self.req_cache) > 512:
            self.req_cache.clear()
        self.req_cache[key] = value
        return value


def _library_ids(standard):
    with (ROOT / solver.LIBRARIES[standard]).open(newline='', encoding='utf-8-sig') as stream:
        return {int(r['rufas_id']) for r in csv.DictReader(stream)}


def _data_rufas(day):
    return f'{day.year}:{day.timetuple().tm_yday}'


def _read(path):
    return json.loads((ROOT / path).read_text())


def _alimentos(s, feed_ids, prices):
    """Cópia da configuração de alimentos de exemplo para a rodada."""
    feed = _read(FEED)
    template = dict(feed['feeds'][0])
    library = _library_ids(s['padrao'])
    # A lista da baia de lactação só define quais alimentos ela pode receber. A
    # quantidade de cada um é decidida pelo otimizador em cada reformulação.
    share = 100 / len(feed_ids)
    for ration in feed['rations']:
        if ration['animal_combination'] == 'lac_cow':
            ration['feeds'] = [dict(feed_type=fid, ration_percentage=share) for fid in feed_ids]
        elif s['padrao'] == 'NRC':
            merged = {}
            for item in ration['feeds']:
                ids = NRC_SUBSTITUTES.get(item['feed_type'], (item['feed_type'],))
                for fid in ids:
                    merged[fid] = merged.get(fid, 0.) + item['ration_percentage'] / len(ids)
            ration['feeds'] = [dict(feed_type=k, ration_percentage=v) for k, v in merged.items()]
    feed['feeds'] = [f for f in feed['feeds'] if f['feed_type'] in library]
    needed = list(dict.fromkeys(x['feed_type'] for r in feed['rations'] for x in r['feeds']))
    missing = [fid for fid in needed if fid not in library]
    if missing:
        raise ValueError(f'Alimentos sem composição na biblioteca {s["padrao"]}: {missing}')
    present = {f['feed_type'] for f in feed['feeds']}
    for fid in needed:
        if fid not in present:
            cost = prices.get(fid, template['purchased_feed_cost'])
            feed['feeds'].append(dict(template, feed_type=fid, purchased_feed_cost=round(cost, 6)))
    return feed


def preparar_entradas(s, folder, feed_ids, prices):
    """Grava cópias das entradas de exemplo com o horizonte, o padrão e o intervalo pedidos."""
    folder.mkdir(parents=True, exist_ok=True)
    config = _read(CONFIG)
    config.update(start_date=_data_rufas(START),
                  end_date=_data_rufas(START + datetime.timedelta(days=s['dias_simulacao'] - 1)),
                  nutrient_standard=s['padrao'], set_seed=True)
    animal = _read(ANIMAL)
    animal['ration']['formulation_interval'] = s['intervalo_dieta']
    animal['ration']['user_input'] = True
    feed = _alimentos(s, feed_ids, prices)
    paths = {}
    for name, data in (('config', config), ('animal', animal), ('feed', feed)):
        paths[name] = folder / f'{name}.json'
        paths[name].write_text(json.dumps(data, indent=2))
    metadata = _read(METADATA)
    for name, path in paths.items():
        metadata['files'][name]['path'] = str(path)
    (folder / 'metadata.json').write_text(json.dumps(metadata, indent=2))
    task = _read(TASK)
    task['parallel_workers'] = 1
    task['tasks'][0].update(metadata_file_path=str(folder / 'metadata.json'), output_prefix='acoplado',
                            random_seeds=[s['semente']], log_verbosity='errors')
    (folder / 'task.json').write_text(json.dumps(task, indent=2))
    tm = {'files': {'tasks': {'title': 'Tarefa acoplada', 'description': 'Rodada do otimizador acoplado ao RuFaS.',
                              'path': str(folder / 'task.json'), 'type': 'json', 'properties': 'tasks_properties'}}}
    (folder / 'task_manager_metadata.json').write_text(json.dumps(tm, indent=2))
    return folder / 'task_manager_metadata.json'


class MotorSLSQP:
    """Otimizador da interface: exigências e restrições nativas do RuFaS, resolvido com SciPy/SLSQP."""

    def __init__(self, s):
        self.s = s
        base = solver.Formulator(dict(padrao=s['padrao'], teto_caroco=s['teto_caroco'],
                                      precos_custom=s['precos_custom'], alimentos_bloqueados=s['alimentos_bloqueados'],
                                      premix_composicao=s['premix_composicao']))
        self.feed_ids = [r['id'] for r in base.records]
        self.precos = {r['id']: f.purchase_cost for r, f in zip(base.records, base.feeds)}

    def _formulator(self, cows, temperature, housing):
        return PenFormulator(_cenario_baia(self.s, cows, temperature), cows, temperature, housing)

    def formular(self, cows, temperature, housing, previous):
        result, x = self._formulator(cows, temperature, housing).solve(0, span=1, initial=previous)
        if not result['sucesso']:
            return result, None
        return dict(sucesso=True, ingredientes=result['ingredientes'], custo_total_dia=result['custo_total_dia'],
                    consumo_total_ms=result['consumo_total_ms'], restricoes=result['restricoes'],
                    exigencias=result['diagnosticos']['exigencias'], iteracoes=result['iteracoes'],
                    tempo_segundos=result['tempo_segundos']), x

    def conferir(self, cows, temperature, housing, x):
        _, checks, _, _, _ = self._formulator(cows, temperature, housing).evaluate(x, 0)
        return checks


class MotorPuLP:
    """Modelo NASEM próprio (modelo_pulp.py), com a média das exigências das vacas da baia."""

    def __init__(self, s):
        self.s = s
        self.F, self.preco_premix = modelo_pulp.carregar_alimentos(s['precos_custom'], s['alimentos_bloqueados'])
        if len(self.F) < 2:
            raise ValueError('Selecione pelo menos dois alimentos.')
        self.feed_ids = [int(i) for i in self.F.id_biblioteca]
        self.precos = dict(zip(self.feed_ids, map(float, self.F.preco)))
        self.tetos = {58: s['teto_caroco']} if s['teto_caroco'] is not None else {}

    def exigencias(self, cows):
        vacas = [modelo_pulp.Vaca(pv=c['peso'], pc_mat=c['peso_adulto'], del_=c['del_ref'], gestacao=c['gest'],
                                  leite=c['leite'], gordura=MilkProduction.fat_percent,
                                  proteina=MilkProduction.true_protein_percent, lactose=MilkProduction.lactose_percent,
                                  ganho=max(c['ganho_alvo'], 0.), reservas=max(c['reservas'], 0.), ecc=c['escore'],
                                  multipara=c['paridade'] >= 2) for c in cows]
        req = modelo_pulp.exigencias_media(vacas)
        f = self.s['faixa_consumo']
        return req, (req['IMS_EQ'] * (1 - f), req['IMS_EQ'] * (1 + f))

    def formular(self, cows, temperature, housing, previous):
        req, ims = self.exigencias(cows)
        r = modelo_pulp.formular(req, self.F, self.preco_premix, ims, self.tetos, self.s['orcamento_dia'])
        if not r['sucesso']:
            return r, None
        return dict(sucesso=True, ingredientes=r['ingredientes'], custo_total_dia=r['custo_total_dia'],
                    consumo_total_ms=r['consumo_total_ms'], restricoes=r['restricoes'],
                    exigencias=dict(r['exigencias'], consumo_min=ims[0], consumo_max=ims[1]),
                    iteracoes=r['rodadas'], tempo_segundos=r['tempo_segundos']), r['kg']

    def conferir(self, cows, temperature, housing, kg):
        req, ims = self.exigencias(cows)
        return modelo_pulp.avaliar(req, self.F, kg, ims, self.preco_premix, self.s['orcamento_dia'])


class Acoplamento:
    """Estado da rodada e os dois ganchos instalados no HerdManager do RuFaS."""

    def __init__(self, s, progress=None):
        self.s = s
        self.progress = progress
        self.reformulacoes = []
        self.diario = []
        self.dietas = {}      # pen id -> dieta em uso: estado do otimizador, ração RuFaS e custo R$/vaca/dia
        self.erro = None
        self.inicio = time.monotonic()
        self.motor = MotorPuLP(s) if s['otimizador'] == 'pulp' else MotorSLSQP(s)
        self.feed_ids = self.motor.feed_ids
        self.precos = self.motor.precos

    def notify(self, message):
        if self.progress:
            self.progress(message)

    def formular(self, pen, pen_feeds, temperature, simulation_day):
        pen.reset_milk_production_reduction()
        pen.set_animal_nutritional_requirements(temperature=temperature, available_feeds=pen_feeds)
        attempts = []
        previous = self.dietas.get(pen.id)
        result = x = None
        while True:
            cows = _vacas(pen)
            milk = sum(c['leite'] for c in cows) / len(cows)
            self.notify(f'Dia {simulation_day + 1}: otimizando a dieta da baia {pen.id} ({len(cows)} vacas, '
                        f'leite médio {milk:.1f} kg, {self.s["otimizador"].upper()}).')
            result, x = self.motor.formular(cows, temperature, pen.housing_type,
                                            None if previous is None else previous['x'])
            attempts.append(dict(leite_medio=milk, sucesso=result['sucesso'],
                                 status=result.get('status'), mensagem=result.get('mensagem')))
            if result['sucesso']:
                break
            # Mesma regra do RuFaS: corta 0,25 kg por vaca até o máximo configurado
            # e para se a média da baia ficar abaixo de 15 kg.
            if milk < AnimalModuleConstants.MINIMUM_AVG_PEN_MILK or not pen.reduce_milk_production():
                break
            pen.set_animal_nutritional_requirements(temperature=temperature, available_feeds=pen_feeds)
        event = dict(dia=simulation_day + 1, data=(START + datetime.timedelta(days=simulation_day)).isoformat(),
                     baia=pen.id, vacas=len(pen.animals_in_pen), temperatura=temperature,
                     leite_medio=milk,
                     corte_leite_medio=pen.average_milk_production_reduction,
                     del_medio=sum(c['del_ref'] for c in cows) / len(cows),
                     peso_medio=sum(c['peso'] for c in cows) / len(cows),
                     prenhes=sum(c['gest'] is not None for c in cows), tentativas=attempts)
        if result['sucesso']:
            ration = {i['id']: i['kg_ms'] for i in result['ingredientes'] if i['id'] != 999}
            self.dietas[pen.id] = dict(x=x, racao=ration, custo=result['custo_total_dia'])
            event.update(sucesso=True, dieta=result['ingredientes'], custo_vaca_dia=result['custo_total_dia'],
                         consumo_ms=result['consumo_total_ms'], restricoes=result['restricoes'],
                         exigencias=result['exigencias'], iteracoes=result['iteracoes'],
                         tempo_segundos=result['tempo_segundos'])
        elif previous is not None:
            ration = dict(previous['racao'])
            event.update(sucesso=False, dieta_mantida=True, custo_vaca_dia=previous['custo'],
                         mensagem=result['mensagem'],
                         violacoes=result.get('violacoes', []))
        else:
            self.erro = dict(dia=simulation_day + 1, baia=pen.id, mensagem=result['mensagem'], detalhe=result)
            self.reformulacoes.append(dict(event, sucesso=False, mensagem=result['mensagem']))
            raise RuntimeError('Sem dieta viável na primeira reformulação: ' + result['mensagem'])
        pen.ration = ration
        pen.set_animal_nutritional_supply(feeds_used=pen_feeds, ration_formulation=ration)
        _, evaluation = NutritionEvaluator.evaluate_nutrition_supply(
            pen.average_nutrition_requirements, pen.average_nutrition_supply, True)
        pen.average_nutrition_evaluation = (evaluation if pen.is_populated
                                            else NutritionEvaluationResults.make_empty_evaluation_results())
        self.reformulacoes.append(event)

    def registrar_dia(self, herd, rufas_time, temperature):
        day = rufas_time.simulation_day
        milking = [c for c in herd.cows if c.is_milking]
        milk = sum(c.milk_production.daily_milk_produced for c in milking)
        cost = 0.0
        check = None
        for pen in herd.all_pens:
            is_lactating_pen = pen.animal_combination is AnimalCombination.LAC_COW
            if not is_lactating_pen or not pen.is_populated or pen.id not in self.dietas:
                continue
            diet = self.dietas[pen.id]
            cost += diet['custo'] * len(pen.animals_in_pen)
            if self.s['conferencia_diaria']:
                checks = self.motor.conferir(_vacas(pen), temperature, pen.housing_type, diet['x'])
                residual = [c['folga'] / c['escala'] for c in checks]
                worst = min(range(len(residual)), key=lambda i: residual[i])
                if check is not None and check['menor_residuo'] <= residual[worst]:
                    continue  # com mais de uma baia de lactação, registra a pior
                check = dict(menor_residuo=float(residual[worst]), restricao=checks[worst]['nome'],
                             atende=all(c['atendida'] for c in checks),
                             falhas=[dict(nome=c['nome'], folga=c['folga'], unidade=c['unidade'])
                                     for c in checks if not c['atendida']])
        lac = sum(len(p.animals_in_pen) for p in herd.all_pens if p.animal_combination is AnimalCombination.LAC_COW)
        self.diario.append(dict(dia=day + 1, data=(START + datetime.timedelta(days=day)).isoformat(),
                                vacas_lactacao=len(milking), vacas_baia_lactacao=lac, leite_rebanho=milk,
                                leite_medio=milk / len(milking) if milking else 0.0, custo_rebanho=cost,
                                del_medio=sum(c.days_in_milk for c in milking) / len(milking) if milking else 0.0,
                                conferencia=check))
        if (day + 1) % 10 == 0 or day + 1 == self.s['dias_simulacao']:
            self.notify(f'Simulação RuFaS: dia {day + 1} de {self.s["dias_simulacao"]}; '
                        f'{time.monotonic() - self.inicio:.0f}s.')

    @contextlib.contextmanager
    def instalado(self):
        original_formulation = HerdManager._reformulate_ration_single_pen
        original_daily = HerdManager.execute_daily_routines
        state = self

        def reformulate(herd, pen, pen_available_feeds, current_temperature, simulation_day):
            if pen.animal_combination is not AnimalCombination.LAC_COW:
                return original_formulation(herd, pen, pen_available_feeds, current_temperature, simulation_day)
            return state.formular(pen, pen_available_feeds, current_temperature, simulation_day)

        def daily(herd, available_feeds, rufas_time, weather):
            output = original_daily(herd, available_feeds, rufas_time, weather)
            temperature = weather.get_current_day_conditions(time=rufas_time).mean_air_temperature
            state.registrar_dia(herd, rufas_time, temperature)
            return output

        HerdManager._reformulate_ration_single_pen = reformulate
        HerdManager.execute_daily_routines = daily
        try:
            yield self
        finally:
            HerdManager._reformulate_ration_single_pen = original_formulation
            HerdManager.execute_daily_routines = original_daily


def resumo(state):
    s = state.s
    days = state.diario
    milk = sum(d['leite_rebanho'] for d in days)
    cost = sum(d['custo_rebanho'] for d in days)
    checked = [d for d in days if d['conferencia']]
    return dict(otimizador=OTIMIZADORES[s['otimizador']], dias_simulados=len(days), leite_total_kg=milk,
                custo_total=cost,
                receita_total=milk * s['preco_leite_kg'], margem_total=milk * s['preco_leite_kg'] - cost,
                custo_por_kg_leite=cost / milk if milk else None,
                media_vacas_lactacao=sum(d['vacas_lactacao'] for d in days) / len(days) if days else 0,
                media_leite_vaca=milk / sum(d['vacas_lactacao'] for d in days) if days and milk else 0,
                reformulacoes=len(state.reformulacoes),
                reformulacoes_sem_dieta_nova=sum(not r['sucesso'] for r in state.reformulacoes),
                dias_conferidos=len(checked),
                dias_exigencia_nao_atendida=sum(not d['conferencia']['atende'] for d in checked))


def avisos(s):
    pulp_notes = [] if s['otimizador'] != 'pulp' else [
        'Otimizador PuLP: modelo NASEM próprio (modelo_pulp.py, mesmas equações de dieta_pulp.py), com a média das '
        'exigências das vacas da baia; consumo entre ±{:.0%} do previsto pela Eq. 2-1.'.format(s['faixa_consumo']),
        'PuLP: o ganho de peso-alvo e a variação de reservas de cada vaca vêm das fórmulas de crescimento do RuFaS '
        '(perda de reservas entra como zero); Tifton 85 usa a composição de "Bermudagrass hay" (código 18), '
        'como no script.']
    return pulp_notes + [
        'Rebanho, curva de lactação, reprodução, descarte e clima vêm do exemplo animals_only do RuFaS '
        '(rebanho de 100 vacas, clima de exemplo a partir de 01/01/2013).',
        'Somente a baia de vacas em lactação recebe a dieta do otimizador; as demais baias seguem a ração nativa.',
        'O leite é o da simulação: no RuFaS a dieta não aumenta a produção; quando não há dieta viável, o leite '
        'é cortado em 0,25 kg por vaca até o máximo configurado, como no fluxo nativo.',
        'A dieta é formulada para as vacas presentes no dia da reformulação e mantida até a próxima; a conferência '
        'diária mostra os dias em que a média da baia deixou de ser atendida.',
        'O premix fixo entra no custo e no otimizador, mas não é passado ao RuFaS, '
        'por não ter código na biblioteca de alimentos.',
        'Estoques não são aplicados neste modo; os limites por alimento e o teto de caroço são aplicados.']


def simular_acoplado(scenario, progress=None):
    s = validar(scenario)
    from RUFAS.task_manager import TaskManager
    from RUFAS.output_manager import LogVerbosity
    state = Acoplamento(s, progress)
    run_id = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
    folder = ROOT / 'output' / 'acoplado' / run_id
    metadata = preparar_entradas(s, folder / 'entradas', state.feed_ids, state.precos)
    state.notify('Iniciando a simulação RuFaS com o otimizador acoplado.')
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        with state.instalado():
            TaskManager().start(metadata_path=metadata, verbosity=LogVerbosity('errors'), exclude_info_maps=True,
                                output_directory=folder / 'rufas', logs_directory=folder / 'logs',
                                clear_output_directory=False, produce_graphics=False, suppress_log_files=False,
                                metadata_depth_limit=None)
    finally:
        os.chdir(cwd)
    base = dict(modo='acoplado', padrao=s['padrao'], otimizador=s['otimizador'],
                motor='RuFaS (simulação animals_only) + ' + OTIMIZADORES[s['otimizador']],
                cenario=s, pasta=str(folder), inicio=START.isoformat(), reformulacoes=state.reformulacoes,
                diario=state.diario, tempo_segundos=time.monotonic() - state.inicio, avisos=avisos(s),
                proveniencia=dict(modelo=MODEL, entradas=[METADATA, CONFIG, ANIMAL, FEED, TASK]))
    if state.erro is not None:
        return dict(base, sucesso=False, status='sem_dieta_inicial', mensagem=state.erro['mensagem'], erro=state.erro)
    if len(state.diario) < s['dias_simulacao']:
        errors = sorted((folder / 'logs').glob('*errors*.json'))
        detail = errors[-1].read_text()[-3000:] if errors else ''
        return dict(base, sucesso=False, status='simulacao_interrompida',
                    mensagem=f'A simulação parou no dia {len(state.diario)} de {s["dias_simulacao"]}. '
                             'Consulte os logs da rodada.', detalhe_tecnico=detail)
    out = dict(base, sucesso=True, resumo=resumo(state))
    (folder / 'resultado.json').write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return out


def main():
    parser = argparse.ArgumentParser(description='Otimizador da interface acoplado à simulação do RuFaS.')
    parser.add_argument('--padrao', default='NASEM', choices=['NASEM', 'NRC'])
    parser.add_argument('--dias', type=int, default=90)
    parser.add_argument('--intervalo', type=int, default=30)
    parser.add_argument('--semente', type=int, default=42)
    parser.add_argument('--otimizador', choices=list(OTIMIZADORES), default=None,
                        help='pulp (padrão no NASEM) ou slsqp (padrão no NRC)')
    parser.add_argument('--sem-conferencia', action='store_true', help='não confere a dieta todos os dias')
    args = parser.parse_args()
    result = simular_acoplado(dict(padrao=args.padrao, dias_simulacao=args.dias, intervalo_dieta=args.intervalo,
                                   semente=args.semente, conferencia_diaria=not args.sem_conferencia,
                                   otimizador=args.otimizador),
                              progress=lambda m: print(m, file=sys.stderr, flush=True))
    if not result['sucesso']:
        print(json.dumps({k: result[k] for k in ('status', 'mensagem')}, ensure_ascii=False, indent=2))
        sys.exit(1)
    r = result['resumo']
    print(r['otimizador'])
    print(f"Dias: {r['dias_simulados']} | reformulações: {r['reformulacoes']} | "
          f"leite: {r['leite_total_kg']:.0f} kg | custo: R$ {r['custo_total']:.2f} | "
          f"R$ {r['custo_por_kg_leite']:.3f}/kg de leite")
    for e in result['reformulacoes']:
        print(f"  dia {e['dia']:>3}: {e['vacas']} vacas, leite {e['leite_medio']:.1f} kg, DEL {e['del_medio']:.0f}, "
              f"custo R$ {e['custo_vaca_dia']:.2f}/vaca/dia, {'nova dieta' if e['sucesso'] else 'dieta mantida'}")
    print('Arquivo:', Path(result['pasta']) / 'resultado.json')


if __name__ == '__main__':
    main()
