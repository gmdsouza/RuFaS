"""Regressões do modo acoplado: otimizador da interface dentro da simulação completa do RuFaS."""
import json
import tempfile
import unittest
from pathlib import Path

from rufas_acoplado import (NRC_SUBSTITUTES, Acoplamento, _library_ids, preparar_entradas, simular_acoplado,
                            validar)
from RUFAS.biophysical.animal.herd_manager import HerdManager


class CoupledSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = (HerdManager._reformulate_ration_single_pen, HerdManager.execute_daily_routines)
        cls.result = simular_acoplado({'dias_simulacao': 20, 'intervalo_dieta': 10})

    def test_runs_whole_horizon_and_reformulates_on_rufas_schedule(self):
        r = self.result
        self.assertTrue(r['sucesso'], r.get('mensagem'))
        self.assertEqual(r['otimizador'], 'pulp')
        self.assertEqual([d['dia'] for d in r['diario']], list(range(1, 21)))
        self.assertEqual([e['dia'] for e in r['reformulacoes']], [1, 11])
        self.assertTrue(all(e['sucesso'] for e in r['reformulacoes']))

    def test_optimized_diet_meets_all_constraints_on_reformulation_day(self):
        for e in self.result['reformulacoes']:
            self.assertTrue(all(c['atendida'] for c in e['restricoes']))
            self.assertGreater(e['vacas'], 0)
            self.assertAlmostEqual(sum(i['custo_dia'] for i in e['dieta']), e['custo_vaca_dia'], places=6)

    def test_milk_comes_from_simulation_not_from_a_target(self):
        first = self.result['reformulacoes'][0]
        self.assertGreater(first['leite_medio'], 15)  # dia 1 usa a curva de Wood, não leite zero
        r = self.result['resumo']
        self.assertAlmostEqual(r['leite_total_kg'], sum(d['leite_rebanho'] for d in self.result['diario']))
        self.assertGreater(len({round(d['leite_rebanho'], 3) for d in self.result['diario']}), 1)

    def test_cost_is_cost_per_cow_times_cows_in_the_lactating_pen(self):
        events = {e['dia']: e for e in self.result['reformulacoes']}
        for d in self.result['diario']:
            diet = events[1] if d['dia'] < 11 else events[11]
            self.assertAlmostEqual(d['custo_rebanho'], diet['custo_vaca_dia'] * d['vacas_baia_lactacao'])

    def test_hooks_are_removed_after_the_run(self):
        current = (HerdManager._reformulate_ration_single_pen, HerdManager.execute_daily_routines)
        self.assertEqual(current, self.original)

    def test_infeasible_first_diet_tries_rufas_milk_cut_then_stops(self):
        cost = self.result['reformulacoes'][0]['custo_vaca_dia']
        r = simular_acoplado({'dias_simulacao': 5, 'intervalo_dieta': 5, 'orcamento_dia': cost - 5})
        self.assertFalse(r['sucesso'])
        self.assertEqual(r['status'], 'sem_dieta_inicial')
        self.assertGreater(len(r['reformulacoes'][0]['tentativas']), 1)

    def test_pulp_diet_comes_from_modelo_pulp_and_is_checked_daily_with_it(self):
        first = self.result['reformulacoes'][0]
        codes = {c['codigo'] for c in first['restricoes']}
        self.assertTrue({'energia', 'pm', 'amido_max', 'fdn_mais_2fdnf', 'sulfur_min'} <= codes)
        self.assertIn('Premix (fixo)', [i['nome'] for i in first['dieta']])
        self.assertTrue(all(d['conferencia'] is not None for d in self.result['diario']))

    def test_slsqp_engine_still_available(self):
        r = simular_acoplado({'dias_simulacao': 6, 'intervalo_dieta': 3, 'otimizador': 'slsqp'})
        self.assertTrue(r['sucesso'], r.get('mensagem'))
        self.assertEqual([e['dia'] for e in r['reformulacoes']], [1, 4])
        self.assertTrue(all(c['atendida'] for e in r['reformulacoes'] for c in e['restricoes']))

    def test_default_optimizer_by_standard(self):
        self.assertEqual(validar({})['otimizador'], 'pulp')
        self.assertEqual(validar({'padrao': 'NRC'})['otimizador'], 'slsqp')
        with self.assertRaises(ValueError):
            validar({'padrao': 'NRC', 'otimizador': 'pulp'})

    def test_validation(self):
        for bad in ({'intervalo_dieta': 40, 'dias_simulacao': 30}, {'dias_simulacao': 731}, {'leite_kg': 40},
                    {'padrao': 'CNCPS'}, {'otimizador': 'cplex'}, {'faixa_consumo': 1.5}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validar(bad)

    def test_nrc_inputs_only_use_nrc_library_feeds(self):
        s = validar({'padrao': 'NRC'})
        state = Acoplamento(s)
        with tempfile.TemporaryDirectory() as folder:
            preparar_entradas(s, Path(folder), state.feed_ids, state.precos)
            feed = json.loads((Path(folder) / 'feed.json').read_text())
        library = _library_ids('NRC')
        used = {x['feed_type'] for r in feed['rations'] for x in r['feeds']}
        self.assertTrue(used <= library)
        self.assertFalse(used & set(NRC_SUBSTITUTES))
        for ration in feed['rations']:
            self.assertAlmostEqual(sum(x['ration_percentage'] for x in ration['feeds']), 100)
        lac = next(r for r in feed['rations'] if r['animal_combination'] == 'lac_cow')
        self.assertEqual(sorted(x['feed_type'] for x in lac['feeds']), sorted(state.feed_ids))


if __name__ == '__main__':
    unittest.main()
