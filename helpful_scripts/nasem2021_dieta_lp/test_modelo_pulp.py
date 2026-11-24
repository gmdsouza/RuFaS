"""Confere que modelo_pulp.py reproduz dieta_pulp.py e que avaliar() usa as mesmas restrições."""
import contextlib
import io
import runpy
import unittest
from dataclasses import replace
from pathlib import Path

import modelo_pulp as M

SCRIPT = Path(__file__).resolve().parent / "dieta_pulp.py"


class ModeloPulpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with contextlib.redirect_stdout(io.StringIO()):
            cls.script = runpy.run_path(str(SCRIPT))
        cls.F, cls.preco_premix = M.carregar_alimentos()
        cls.req = M.exigencias(M.TABELA_21_1)
        cls.ims = (min(cls.req["IMS_EQ"], M.IMS_TABELA), max(cls.req["IMS_EQ"], M.IMS_TABELA))
        cls.res = M.formular(cls.req, cls.F, cls.preco_premix, cls.ims)

    def test_requirements_match_script(self):
        for nome in ("EXIG_ELL", "PM_FIXA", "CA_FIXO", "P_FIXO", "IMS_EQ", "PB_LEITE"):
            self.assertAlmostEqual(self.req[nome], self.script[nome], places=10, msg=nome)

    def test_same_diet_and_cost_as_script(self):
        self.assertTrue(self.res["sucesso"])
        self.assertAlmostEqual(self.res["custo_total_dia"], self.script["custo"], places=6)
        self.assertEqual(self.res["rodadas"], self.script["rodada"])
        for alimento, kg in self.script["kg"].items():
            self.assertAlmostEqual(self.res["kg"][alimento], kg, places=6, msg=alimento)

    def test_binding_constraints_match_script(self):
        script = {n for n, c in self.script["model"].constraints.items() if abs(c.slack) < 1e-3}
        ours = {c["codigo"] for c in self.res["restricoes"]
                if abs(c["folga"] * (self.res["consumo_total_ms"] if c["unidade"] == "% MS" else 1)) < 1e-3}
        self.assertEqual(ours, script)

    def test_evaluating_the_formulated_diet_gives_same_slacks(self):
        checks = M.avaliar(self.req, self.F, self.res["kg"], self.ims, self.preco_premix)
        self.assertTrue(all(c["atendida"] for c in checks))
        for a, b in zip(checks, self.res["restricoes"]):
            self.assertAlmostEqual(a["folga"], b["folga"], places=4, msg=a["nome"])

    def test_evaluation_detects_higher_milk(self):
        more = M.exigencias(replace(M.TABELA_21_1, leite=60.0))
        checks = {c["codigo"]: c for c in M.avaliar(more, self.F, self.res["kg"], self.ims, self.preco_premix)}
        self.assertFalse(checks["energia"]["atendida"])
        self.assertFalse(checks["pm"]["atendida"])

    def test_average_of_identical_cows_is_the_cow(self):
        media = M.exigencias_media([M.TABELA_21_1] * 3)
        for k, v in self.req.items():
            self.assertAlmostEqual(media[k], v)

    def test_open_cow_has_no_pregnancy_terms(self):
        vazia = M.exigencias(replace(M.TABELA_21_1, gestacao=None))
        self.assertLess(vazia["PM_FIXA"], self.req["PM_FIXA"])
        self.assertLess(vazia["CA_FIXO"], self.req["CA_FIXO"])

    def test_cap_block_and_budget(self):
        r = M.formular(self.req, self.F, self.preco_premix, self.ims, tetos={169: 2.0})
        self.assertLessEqual(r["kg"]["Soja grão"], 2.0 + 1e-9)
        self.assertGreater(r["custo_total_dia"], self.res["custo_total_dia"])
        F, p = M.carregar_alimentos(bloqueados=[169])
        self.assertNotIn("Soja grão", F.index)
        r = M.formular(self.req, self.F, self.preco_premix, self.ims, orcamento=30.0)
        self.assertFalse(r["sucesso"])


if __name__ == "__main__":
    unittest.main()
