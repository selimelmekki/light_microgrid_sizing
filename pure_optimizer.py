from pyomo.environ import *
from pyomo.opt import ProblemFormat
#from microgrid_simulator.core import CommunityGrid
from grid import Grid
import time
import sys
from datetime import timedelta
import pandas as pd
import matplotlib.pyplot as plt
import logging

class PureOptimizer:
    """
    Class that instantiates the pure optimization algorithm
    """

    def __init__(self, microgrid, selected_days, extracted_series, extracted_weights, config, initial_state):
        self.microgrid = microgrid
        self.evaluations_points = []
        self.evaluations_objective = []
        # self._sized_device_lower_bounds = []
        # self._sized_device_upper_bounds = []
        self.selected_days = selected_days
        self.extracted_series = extracted_series
        self.extracted_weights = extracted_weights
        self.sizing_config=config
        self.initial_state = initial_state
        self.solver_name = "gurobi"
        self._create_model()
        self.optimal_sizing = {}

    def _create_model(self):
        t0_building = time.time()
        self.model = ConcreteModel()
        self._create_sets()
        self._create_parameters()
        self._create_variables()
        self._create_constraints()
        self._create_objective()
        self.building_duration = time.time() - t0_building
        print("Building time: %.2fs" % self.building_duration)

    def _create_sets(self):
        self.working_series = self.extracted_series[0].resample('60T').mean()  # work with 1h time step (imposed)
        # remove last day because it is added for computational reasons
        self.working_series = self.working_series.iloc[:-24]

        if self.sizing_config.full_sizing or self.sizing_config.multi_stage_sizing:
            self.number_periods = len(self.working_series) * self.sizing_config.investment_horizon
            current_series = self.working_series.copy()
            for y in range(self.sizing_config.investment_horizon - 1):
                current_series.index += timedelta(days=self.sizing_config.num_representative_days - 1)
                self.working_series = pd.concat([self.working_series, current_series])
        else:
            self.number_periods = len(self.working_series)

        self.model.periods = Set(initialize=range(self.number_periods))
        self.model.investment_horizon = Set(initialize=range(self.sizing_config.investment_horizon))

        self.entity = Grid(self.microgrid)

        if self.sizing_config.grid_connection_cost:
            for l in self.entity.non_flexible_loads:
                self.actual_connection_type = l.connection_type
                self.actual_grid_connection_capacity = l.capacity
            connection_types = self.microgrid['connection_types']
            self.grid_capacity_options_parameters = {}
            n_c = len(connection_types)
            for c in range(n_c):
                if self.actual_connection_type == connection_types[c]['type']:
                    for i in range(c + 1):
                        p_c = connection_types[c - i]['type']
                        self.grid_capacity_options_parameters[p_c] = connection_types[c - i]
            # the first element of possible_connection_investment is the actual connection type but it is also possible
            # to invest in a bigger connection capacity without changing connection type (only the proportional
            # connection cost is in charge --> fix cost = 0).
            self.grid_capacity_options_parameters[self.actual_connection_type]['fix_connection_cost'] = 0
            self.model.grid_capacity_options = Set(dimen=1, initialize=set(self.grid_capacity_options_parameters))

        indices = set()
        for s in self.entity.storages:
            indices.add(s)
        self.model.storages = Set(dimen=1, initialize=indices)

        indices = set()
        for s in self.entity.non_steerable_generators:
            indices.add(s)
        self.model.non_steerable_generators = Set(dimen=1, initialize=indices)

        indices = set()
        for s in self.entity.inverters:
            indices.add(s)
        self.model.inverters = Set(dimen=1, initialize=indices)

        indices = set()
        for s in self.entity.h2_storages:
            indices.add(s)
        self.model.h2_storages = Set(dimen=1, initialize=indices)

        indices = set()
        for s in self.entity.h2_tanks:
            indices.add(s)
        self.model.h2_tanks = Set(dimen=1, initialize=indices)

        indices = set()
        for s in self.entity.steerable_generators:
            indices.add(s)
        self.model.steerable_generators = Set(dimen=1, initialize=indices)

    def _create_parameters(self):
        delta_t_in_hours = 1  # imposed
        self.delta_t = delta_t_in_hours

        weights_list = []
        for key, value in self.extracted_weights[0].items():
            weights_list.append(value)

        # we have a list of weights per day, we want a list of weights per period
        self.weights = []
        hour = 0
        se = 0

        if self.sizing_config.full_sizing or self.sizing_config.multi_stage_sizing:
            initial_weight_list = weights_list
            for y in range(self.sizing_config.investment_horizon - 1):
                weights_list = weights_list + initial_weight_list

        for p in range(self.number_periods):
            if hour == 24:
                se += 1
                hour = 0
            self.weights.append(weights_list[se])
            hour += 1

        # for now number_of_cycles is a parameter (1 cycle per day)
        self.number_of_cycles = {}
        periods_per_year = self.number_periods / self.sizing_config.investment_horizon
        for n in range(self.sizing_config.investment_horizon):
            self.number_of_cycles[n] = [0.0] * int(self.number_periods)
            for p in range(int(self.number_periods)):
                if p < n * periods_per_year:
                    self.number_of_cycles[n][p] = 0
                else:
                    if p % 24 == 6:
                        self.number_of_cycles[n][p] = self.number_of_cycles[n][p - 1] + self.weights[p]
                    else:
                        self.number_of_cycles[n][p] = self.number_of_cycles[n][p - 1]

        # aggregate all consumption into one list, must be considered separately when considering several entities
        self.total_consumption = [0.0] * self.number_periods
        for s in self.entity.non_flexible_loads:
            for p in range(self.number_periods):
                self.total_consumption[p] += self.working_series[s.name][p] * s.capacity

        self.non_steerable_generation_per_kW = {}
        for g in self.entity.non_steerable_generators:
            self.non_steerable_generation_per_kW[g] = [0.0] * self.number_periods
            for p in range(self.number_periods):
                self.non_steerable_generation_per_kW[g][p] += self.working_series[g.name][p]

        self.purchase_price = list(self.working_series['purchase_price'])
        self.sale_price = list(self.working_series['sale_price'])

    def _create_variables(self):
        # cost variables
        self.model.investment_cost = Var(within=NonNegativeReals)
        if self.sizing_config.full_sizing:
            self.model.net_operation_cost = Var(self.model.investment_horizon, within=NonNegativeReals)
            self.model.reinvestment_cost = Var(self.model.investment_horizon, within=NonNegativeReals)
            self.model.bin_reinvestment = Var(self.model.investment_horizon, within=Binary)
            self.model.reinv_storages_capacity = Var(self.model.storages, self.model.investment_horizon,
                                                     within=NonNegativeReals)
            self.model.z_product = Var(self.model.storages, self.model.investment_horizon, within=NonNegativeReals)
        elif self.sizing_config.multi_stage_sizing:
            self.model.net_operation_cost = Var(self.model.investment_horizon, within=NonNegativeReals)
            self.model.reinvestment_cost = Var(self.model.investment_horizon, within=NonNegativeReals)
            self.model.reinv_storages_capacity = Var(self.model.storages, self.model.investment_horizon,
                                                     within=NonNegativeReals)
        else:
            self.model.net_operation_cost = Var(within=NonNegativeReals)

        # operation variables
        self.model.charge = Var(self.model.storages, self.model.periods, within=NonNegativeReals)
        self.model.discharge = Var(self.model.storages, self.model.periods, within=NonNegativeReals)
        self.model.curtail = Var(self.model.non_steerable_generators, self.model.periods, within=NonNegativeReals)

        self.model.soc = Var(self.model.storages, self.model.periods, within=NonNegativeReals)

        self.model.exp_grid = Var(self.model.periods, within=NonNegativeReals)  # Energy sent to the grid
        self.model.imp_grid = Var(self.model.periods, within=NonNegativeReals)  # Energy imported from the grid

        self.model.non_steerable_generations = Var(self.model.non_steerable_generators, self.model.periods,
                                                   within=NonNegativeReals)
        self.model.steerable_generations = Var(self.model.steerable_generators, self.model.periods,
                                               within=NonNegativeReals)
        self.model.h2_energy_stored = Var(self.model.h2_storages, self.model.periods, within=NonNegativeReals)
        self.model.h2_charge = Var(self.model.h2_storages, self.model.periods, within=NonNegativeReals)
        self.model.h2_discharge = Var(self.model.h2_storages, self.model.periods, within=NonNegativeReals)

        # sizing variables
        self.model.non_steerable_generators_capacity = Var(self.model.non_steerable_generators, within=NonNegativeReals)
        self.model.storages_capacity = Var(self.model.storages, within=NonNegativeReals)
        self.model.usable_storages_capacity = Var(self.model.storages, self.model.periods, within=NonNegativeReals)
        self.model.inverters_capacity = Var(self.model.inverters, within=NonNegativeReals)
        self.model.h2_capacity = Var(self.model.h2_storages, within=NonNegativeReals)
        self.model.h2_tanks_capacity = Var(self.model.h2_tanks, within=NonNegativeReals)
        self.model.steerable_generators_capacity = Var(self.model.steerable_generators, within=NonNegativeReals)

        if self.sizing_config.grid_connection_cost and self.sizing_config.grid_tied:
            self.model.grid_capacity_bin = Var(self.model.grid_capacity_options, within=Binary)
            self.model.delta = Var(within=Binary)  # used to compute connection cost if P_c > L_c
            self.model.prop_connection_cost = Var(within=NonNegativeReals)
            # self.model.connection_fee = Var(within=NonNegativeReals)
            self.model.total_generators_capacity = Var(within=NonNegativeReals)

    def production_expr(self, m, p):
        delta_t = self.delta_t
        production_expr = 0
        for s in self.entity.storages:
            production_expr += m.discharge[s, p] * delta_t
        for g in self.entity.non_steerable_generators:
            production_expr += m.non_steerable_generations[g, p] * delta_t
        for g in self.entity.steerable_generators:
            production_expr += m.steerable_generations[g, p] * delta_t
        for h in self.entity.h2_storages:
            production_expr += m.h2_discharge[h, p] * delta_t
        return production_expr

    def consumption_expr(self, m, p):
        delta_t = self.delta_t
        consumption_expr = 0
        for s in self.entity.storages:
            consumption_expr += m.charge[s, p] * delta_t
        for h in self.entity.h2_storages:
            consumption_expr += m.h2_charge[h, p] * delta_t

        consumption_expr += self.total_consumption[p] * delta_t
        return consumption_expr

    def _create_constraints(self):

        def investment_cost_cstr(m):
            rhs = 0
            for g in self.entity.non_steerable_generators:
                rhs += m.non_steerable_generators_capacity[g] * g.capex

            for i in self.entity.inverters:
                rhs += m.inverters_capacity[i] * i.capex

            for g in self.entity.steerable_generators:
                rhs += m.steerable_generators_capacity[g] * g.capex

            for s in self.entity.storages:
                rhs += m.storages_capacity[s] * s.capex

            for h in self.entity.h2_storages:
                rhs += m.h2_capacity[h] * h.capex

            for t in self.entity.h2_tanks:
                rhs += m.h2_tanks_capacity[t] * t.capex

            if self.sizing_config.grid_connection_cost and self.sizing_config.grid_tied:
                rhs += sum(m.grid_capacity_bin[c] * self.grid_capacity_options_parameters[c]['fix_connection_cost']
                           for c in m.grid_capacity_options)
                rhs += m.prop_connection_cost

            return m.investment_cost == rhs

        def delta_cstr_1(m):
            M = 10000
            return m.total_generators_capacity >= self.actual_grid_connection_capacity - M * (1 - m.delta)

        def delta_cstr_2(m):
            M = 10000
            return m.total_generators_capacity <= self.actual_grid_connection_capacity + M * m.delta

        def prop_connection_cost_cstr_1_1(m):
            M = 10000
            unit_proportional_connection_cost = sum(m.grid_capacity_bin[c]
                                                    * self.grid_capacity_options_parameters[c][
                                                        'proportional_connection_cost']
                                                    for c in m.grid_capacity_options)
            lhs = unit_proportional_connection_cost * (m.total_generators_capacity
                                                       - self.actual_grid_connection_capacity) - M * (1 - m.delta)
            return lhs <= m.prop_connection_cost

        def prop_connection_cost_cstr_1_2(m):
            M = 10000
            unit_proportional_connection_cost = sum(m.grid_capacity_bin[c]
                                                    * self.grid_capacity_options_parameters[c][
                                                        'proportional_connection_cost']
                                                    for c in m.grid_capacity_options)
            rhs = unit_proportional_connection_cost * (m.total_generators_capacity
                                                       - self.actual_grid_connection_capacity) + M * (1 - m.delta)
            return m.prop_connection_cost <= rhs

        def prop_connection_cost_cstr_2_1(m):
            M = 10000
            lhs = - M * m.delta
            return lhs <= m.prop_connection_cost

        def prop_connection_cost_cstr_2_2(m):
            M = 10000
            rhs = M * m.delta
            return m.prop_connection_cost <= rhs

        def operation_cost_cstr(m):
            purchase_price = self.purchase_price
            sale_price = self.sale_price
            weights = self.weights
            rhs = 0
            for g in self.entity.non_steerable_generators:
                rhs += m.non_steerable_generators_capacity[g] * g.opex

            for i in self.entity.inverters:
                rhs += m.inverters_capacity[i] * i.opex

            for g in self.entity.steerable_generators:
                rhs += m.steerable_generators_capacity[g] * g.opex

            for s in self.entity.storages:
                rhs += m.storages_capacity[s] * s.opex

            for h in self.entity.h2_storages:
                rhs += m.h2_capacity[h] * h.opex

            for t in self.entity.h2_tanks:
                rhs += m.h2_tanks_capacity[t] * t.opex

            rhs += sum(
                (m.imp_grid[p] * purchase_price[p] - m.exp_grid[p] * sale_price[p]) * weights[p] for p in m.periods)

            for g in self.entity.steerable_generators:
                rhs += sum(m.steerable_generations[g, p] * self.delta_t * g.fuel_price / g.fuel_efficiency * weights[p] for p in m.periods)

            if self.sizing_config.grid_connection_cost and self.sizing_config.grid_tied:
                rhs += sum(m.grid_capacity_bin[c] * self.grid_capacity_options_parameters[c]['annual_fee']
                           for c in m.grid_capacity_options)
                rhs += sum(m.grid_capacity_bin[c]
                           * self.grid_capacity_options_parameters[c]['annual_injection_fee_per_kva']
                           for c in m.grid_capacity_options) * m.total_generators_capacity
            return m.net_operation_cost == rhs

        def operation_cost_multi_year_cstr(m, n):
            purchase_price = self.purchase_price
            sale_price = self.sale_price
            weights = self.weights
            periods_per_year = self.number_periods / self.sizing_config.investment_horizon
            rhs = 0
            for g in self.entity.non_steerable_generators:
                rhs += m.non_steerable_generators_capacity[g] * g.opex

            for i in self.entity.inverters:
                rhs += m.inverters_capacity[i] * i.opex

            for g in self.entity.steerable_generators:
                rhs += m.steerable_generators_capacity[g] * g.opex

            for s in self.entity.storages:
                rhs += m.storages_capacity[s] * s.opex

            for h in self.entity.h2_storages:
                rhs += m.h2_capacity[h] * h.opex

            for t in self.entity.h2_tanks:
                rhs += m.h2_tanks_capacity[t] * t.opex

            if self.sizing_config.grid_connection_cost and self.sizing_config.grid_tied:
                rhs += sum(m.grid_capacity_bin[c] * self.grid_capacity_options_parameters[c]['annual_fee']
                           for c in m.grid_capacity_options)
                rhs += sum(m.grid_capacity_bin[c]
                           * self.grid_capacity_options_parameters[c]['annual_injection_fee_per_kva']
                           for c in m.grid_capacity_options) * m.total_generators_capacity
            for p in m.periods:
                if (n * periods_per_year) <= p < (n + 1) * periods_per_year:
                    rhs += (m.imp_grid[p] * purchase_price[p] - m.exp_grid[p] * sale_price[p]) * weights[p]
                    for g in self.entity.steerable_generators:
                        rhs += m.steerable_generations[g, p] * self.delta_t * g.fuel_price / g.fuel_efficiency * weights[p]
            return m.net_operation_cost[n] == rhs

        def power_balance_cstr(m, p):
            return m.exp_grid[p] - m.imp_grid[p] == self.production_expr(m, p) - self.consumption_expr(m, p)

        def non_steerable_generations_cstr(m, g, p):
            periods_per_year = self.number_periods / self.sizing_config.investment_horizon
            year_of_period_p = int(p/periods_per_year)
            rhs = self.non_steerable_generation_per_kW[g][p] * m.non_steerable_generators_capacity[g] \
                * (1 + g.progression)**year_of_period_p
            return m.non_steerable_generations[g, p] <= rhs

        def curtailment_cstr(m, g, p):
            rhs = self.non_steerable_generation_per_kW[g][p] * m.non_steerable_generators_capacity[g]
            rhs -= m.non_steerable_generations[g, p]
            return m.curtail[g, p] == rhs

        def steerable_generations_cstr(m, g, p):
            return m.steerable_generations[g, p] <= m.steerable_generators_capacity[g]

        def inverters_cstr(m, p):
            lhs = 0
            lhs += sum(m.non_steerable_generations[g, p] for g in m.non_steerable_generators)
            lhs += sum(m.discharge[s, p] for s in m.storages)
            return lhs <= sum(m.inverters_capacity[i] for i in m.inverters)

        def max_charge_cstr(m, s, p):
            return m.charge[s, p] <= s.max_charge_rate * m.usable_storages_capacity[s, p]

        def max_discharge_cstr(m, s, p):
            return m.discharge[s, p] <= s.max_discharge_rate * m.usable_storages_capacity[s, p]

        def storage_level_rule(m, s, p):
            delta_t = self.delta_t
            if p == 0:
                return m.soc[s, p] == m.storages_capacity[s] / 2
            else:
                rhs = m.charge[s, p] * s.charge_efficiency * delta_t
                rhs -= (m.discharge[s, p] / s.discharge_efficiency) * delta_t
                return m.soc[s, p] == m.soc[s, p - 1] + rhs

        def soc_cstr(m, s, p):
            return m.soc[s, p] <= m.usable_storages_capacity[s, p]

        def usable_storage_capacity_cstr(m, s, p):
            return m.usable_storages_capacity[s, p] <= m.storages_capacity[s]

        def usable_storage_capacity_full_sizing_cstr(m, s, p):
            u_min = s.residual_capacity
            n_c_max = s.max_number_cycle
            rhs = (((u_min - 1) / n_c_max) * self.number_of_cycles[0][p] + 1) * m.storages_capacity[s]
            periods_per_year = self.number_periods / self.sizing_config.investment_horizon
            year_of_period_p = int(p/periods_per_year)
            rhs += sum(m.z_product[s, n] * (1 - u_min) for n in range(year_of_period_p))
            # z = bin_reinvestment * storages_capacity used to linearize product of a binary and continuous variable
            return m.usable_storages_capacity[s, p] == rhs

        def linearization_constraint_1(m, s, n):
            b_cap_upper = 10000
            return m.z_product[s, n] <= b_cap_upper * m.bin_reinvestment[n]

        def linearization_constraint_2(m, s, n):
            return m.z_product[s, n] <= m.storages_capacity[s]

        def linearization_constraint_3(m, s, n):
            b_cap_upper = 10000
            return m.z_product[s, n] >= m.storages_capacity[s] - (1 - m.bin_reinvestment[n]) * b_cap_upper

        def reinvestment_full_sizing_cstr(m, s, n):
            return m.reinvestment_cost[n] == m.z_product[s, n] * s.capex

        def usable_storage_capacity_multi_stage_cstr(m, s, p):
            u_min = s.residual_capacity
            n_c_max = s.max_number_cycle
            periods_per_year = self.number_periods / self.sizing_config.investment_horizon
            year_of_period_p = int(p/periods_per_year)
            rhs = (((u_min - 1) / n_c_max) * self.number_of_cycles[0][p] + 1) * m.storages_capacity[s]
            if year_of_period_p == 0:
                return m.usable_storages_capacity[s, p] == rhs
            else:
                rhs += sum((((u_min - 1) / n_c_max) * self.number_of_cycles[n][p] + 1) * m.reinv_storages_capacity[s, n]
                           for n in range(year_of_period_p))
                return m.usable_storages_capacity[s, p] == rhs

        def reinvestment_multi_stage_cstr(m, s, n):
            return m.reinvestment_cost[n] == m.reinv_storages_capacity[s, n] * s.capex

        def no_reinvestment_year_0_cstr(m, s):
            return m.reinv_storages_capacity[s, 0] == 0

        # def min_battery_capacity_cstr(m, s):
        #     return m.storages_capacity[s] == 10

        def days_decoup_cstr(m, s, p):
            if p % 24 == 0:
                return m.soc[s, p] == 0.5 * m.usable_storages_capacity[s, p]
            else:
                return m.soc[s, p] <= m.usable_storages_capacity[s, p]

        def grid_connection_choice_cstr(m):
            return sum(m.grid_capacity_bin[c] for c in m.grid_capacity_options) <= 1

        def grid_connection_capacity_cstr(m):
            rhs = sum(m.grid_capacity_bin[c] * self.grid_capacity_options_parameters[c]['upper_limit']
                      for c in m.grid_capacity_options)
            return m.total_generators_capacity <= rhs

        def total_generators_capacity_cstr(m):
            rhs = 0
            # for g in self.entity.non_steerable_generators:
            #     total_generators_capacity += m.non_steerable_generators_capacity[g]
            for i in self.entity.inverters:
                rhs += m.inverters_capacity[i]
            for g in self.entity.steerable_generators:
                rhs += m.steerable_generators_capacity[g]

            return m.total_generators_capacity == rhs

        def h2_stored_rule(m, h, p):
            delta_t = self.delta_t
            if p == 0:
                return m.h2_energy_stored[h, p] == 0
            else:
                rhs = m.h2_charge[h, p] * h.charge_efficiency * delta_t
                rhs -= (m.h2_discharge[h, p] / h.discharge_efficiency) * delta_t
                return m.h2_energy_stored[h, p] == m.h2_energy_stored[h, p - 1] + rhs * self.weights[p]

        def h2_max_charge_cstr(m, h, p):
            return m.h2_charge[h, p] <= h.max_charge_rate * m.h2_capacity[h]

        def h2_max_discharge_cstr(m, h, p):
            return m.h2_discharge[h, p] <= h.max_discharge_rate * m.h2_capacity[h]

        def h2_tanks_cstr(m, p):
            lhs = 0
            for h in self.entity.h2_storages:
                lhs += m.h2_energy_stored[h, p]
            rhs = 0
            for t in self.entity.h2_tanks:
                rhs += m.h2_tanks_capacity[t] * t.calorific_value_per_l
            return lhs <= rhs

        def offgrid_import_cstr(m, p):
            return m.imp_grid[p] == 0

        def offgrid_export_cstr(m, p):
            return m.exp_grid[p] == 0

        def inverter_pv_cstr(m):
            total_pvs_capacity = sum(m.non_steerable_generators_capacity[g] for g in self.entity.non_steerable_generators)
            total_inverters_capacity = sum(m.inverters_capacity[i] for i in self.entity.inverters)
            return total_pvs_capacity <= total_inverters_capacity * 2

        self.model.investment_cost_cstr = Constraint(rule=investment_cost_cstr)
        self.model.power_balance_cstr = Constraint(self.model.periods, rule=power_balance_cstr)
        self.model.non_steerable_generations_cstr = Constraint(self.model.non_steerable_generators, self.model.periods,
                                                               rule=non_steerable_generations_cstr)
        self.model.curtailment_cstr = Constraint(self.model.non_steerable_generators, self.model.periods,
                                                 rule=curtailment_cstr)
        self.model.steerable_generations_cstr = Constraint(self.model.steerable_generators, self.model.periods,
                                                           rule=steerable_generations_cstr)
        self.model.inverters_cstr = Constraint(self.model.periods, rule=inverters_cstr)
        self.model.max_charge_cstr = Constraint(self.model.storages, self.model.periods, rule=max_charge_cstr)
        self.model.max_discharge_cstr = Constraint(self.model.storages, self.model.periods, rule=max_discharge_cstr)
        self.model.storage_level_rule = Constraint(self.model.storages, self.model.periods, rule=storage_level_rule)
        self.model.soc_cstr = Constraint(self.model.storages, self.model.periods, rule=soc_cstr)
        self.model.days_decoup_cstr = Constraint(self.model.storages, self.model.periods, rule=days_decoup_cstr)

        self.model.h2_stored_rule = Constraint(self.model.h2_storages, self.model.periods, rule=h2_stored_rule)
        self.model.h2_max_charge_cstr = Constraint(self.model.h2_storages, self.model.periods, rule=h2_max_charge_cstr)
        self.model.h2_max_discharge_cstr = Constraint(self.model.h2_storages, self.model.periods, rule=h2_max_discharge_cstr)
        if len(self.entity.h2_tanks) > 0:
            self.model.h2_tanks_cstr = Constraint(self.model.periods, rule=h2_tanks_cstr)

        self.model.inverter_pv_cstr = Constraint(rule=inverter_pv_cstr)

        if not self.sizing_config.grid_tied:
            self.model.offgrid_import_cstr = Constraint(self.model.periods, rule=offgrid_import_cstr)
            self.model.offgrid_export_cstr = Constraint(self.model.periods, rule=offgrid_export_cstr)

        if self.sizing_config.grid_connection_cost and self.sizing_config.grid_tied:
            self.model.grid_connection_choice_cstr = Constraint(rule=grid_connection_choice_cstr)
            self.model.grid_connection_capacity_cstr = Constraint(rule=grid_connection_capacity_cstr)
            self.model.delta_cstr_1 = Constraint(rule=delta_cstr_1)
            self.model.delta_cstr_2 = Constraint(rule=delta_cstr_2)
            self.model.prop_connection_cost_cstr1_1 = Constraint(rule=prop_connection_cost_cstr_1_1)
            self.model.prop_connection_cost_cstr1_2 = Constraint(rule=prop_connection_cost_cstr_1_2)
            self.model.prop_connection_cost_cstr2_1 = Constraint(rule=prop_connection_cost_cstr_2_1)
            self.model.prop_connection_cost_cstr2_2 = Constraint(rule=prop_connection_cost_cstr_2_2)
            self.model.total_generators_capacity_cstr = Constraint(rule=total_generators_capacity_cstr)

        if self.sizing_config.full_sizing:
            self.model.operation_cost_multi_year_cstr = Constraint(self.model.investment_horizon,
                                                                   rule=operation_cost_multi_year_cstr)
            self.model.usable_storage_capacity_full_sizing_cstr = Constraint(self.model.storages, self.model.periods,
                                                                            rule=usable_storage_capacity_full_sizing_cstr)

            self.model.reinvestment_full_sizing_cstr = Constraint(self.model.storages, self.model.investment_horizon,
                                                      rule=reinvestment_full_sizing_cstr)
            self.model.linearization_constraint_1 = Constraint(self.model.storages, self.model.investment_horizon,
                                                               rule=linearization_constraint_1)
            self.model.linearization_constraint_2 = Constraint(self.model.storages, self.model.investment_horizon,
                                                               rule=linearization_constraint_2)
            self.model.linearization_constraint_3 = Constraint(self.model.storages, self.model.investment_horizon,
                                                               rule=linearization_constraint_3)
            self.model.usable_storage_capacity_cstr = Constraint(self.model.storages, self.model.periods,
                                                                 rule=usable_storage_capacity_cstr)

        elif self.sizing_config.multi_stage_sizing:
            self.model.operation_cost_multi_year_cstr = Constraint(self.model.investment_horizon,
                                                                   rule=operation_cost_multi_year_cstr)
            self.model.reinvestment_multi_stage_cstr = Constraint(self.model.storages, self.model.investment_horizon,
                                                      rule=reinvestment_multi_stage_cstr)
            self.model.usable_storage_capacity_multi_stage_cstr = Constraint(self.model.storages, self.model.periods,
                                                                            rule=usable_storage_capacity_multi_stage_cstr)
            self.model.no_reinvestment_year_0_cstr = Constraint(self.model.storages, rule=no_reinvestment_year_0_cstr)

        else:
            self.model.operation_cost_cstr = Constraint(rule=operation_cost_cstr)
            self.model.usable_storage_capacity_cstr = Constraint(self.model.storages, self.model.periods,
                                                                 rule=usable_storage_capacity_cstr)

    def _create_objective(self):
        def obj_expression(model):
            d = self.sizing_config.discount_rate
            if self.sizing_config.full_sizing or self.sizing_config.multi_stage_sizing:
                return - model.investment_cost - sum(
                    (model.reinvestment_cost[n] + model.net_operation_cost[n]) / (1 + d) ** n for n in
                    self.model.investment_horizon)
            else:
                return - model.investment_cost - sum(
                    model.net_operation_cost / (1 + d) ** n for n in self.model.investment_horizon)

        self.model.obj = Objective(rule=obj_expression, sense=maximize)

    def _optimize_function(self, lower_bounds, upper_bounds, maximum_iterations):
        t0_solve = time.time()
        solver = SolverFactory(self.solver_name)
        self.results = solver.solve(self.model)
        self.model.write(filename="damClearing.lp", format=ProblemFormat.cpxlp,
                         io_options={"symbolic_solver_labels": True})
        self.solving_duration = time.time() - t0_solve
        print("Solving time: %.2fs" % self.solving_duration)
        print(self.results)
        obj = self.model.obj.value()
        PV_cap = []
        Inv_cap = []
        Battery_cap = []
        H2_cap = []
        H2_tank_cap = []
        Genset_cap = []
        results = []
        total_battery_reinvestment = 0.0
        self.model.reinv_storages_capacity.display()
        for device_name in self.entity.devices_names:
            for g in self.entity.non_steerable_generators:
                if g.name == device_name:
                    PV_cap.append(self.model.non_steerable_generators_capacity[g].value)
                    results.append(self.model.non_steerable_generators_capacity[g].value)
            for i in self.entity.inverters:
                if i.name == device_name:
                    Inv_cap.append(self.model.inverters_capacity[i].value)
                    results.append(self.model.inverters_capacity[i].value)
            for g in self.entity.steerable_generators:
                if g.name == device_name:
                    Genset_cap.append(self.model.steerable_generators_capacity[g].value)
                    results.append(self.model.steerable_generators_capacity[g].value)
            for s in self.entity.storages:
                if s.name == device_name:
                    Battery_cap.append(self.model.storages_capacity[s].value)
                    results.append(self.model.storages_capacity[s].value)
                    if self.sizing_config.full_sizing or self.sizing_config.multi_stage_sizing:
                        for n in range(self.sizing_config.investment_horizon):
                            print(total_battery_reinvestment)
                            print(self.model.reinv_storages_capacity[s, n].value)
                            total_battery_reinvestment += self.model.reinv_storages_capacity[s, n].value
            for h in self.entity.h2_storages:
                if h.name == device_name:
                    H2_cap.append(self.model.h2_capacity[h].value)
                    results.append(self.model.h2_capacity[h].value)
            for t in self.entity.h2_tanks:
                if t.name == device_name:
                    H2_tank_cap.append(self.model.h2_tanks_capacity[t].value)
                    results.append(self.model.h2_tanks_capacity[t].value)

        self.evaluations_points.append(results)
        self.evaluations_objective.append(-obj)

        # self.model.delta.display()
        # self.model.prop_connection_cost.display()
        # self.model.connection_fee.display()
        # self.model.charge.display()
        # self.model.soc.display()
        # self.model.usable_storages_capacity.display()
        # self.model.reinv_storages_capacity.display()
        # self.model.bin_reinvestment.display()
        # self.model.reinvestment_cost.display()
        # self.model.curtail.display()
        print("PV = %s kWp" % PV_cap)
        print("Inverter = %s KVA" % Inv_cap)
        print("BESS = %s kWh" % Battery_cap)
        print("BESS reinv = %s kWh" % total_battery_reinvestment)
        print("H2 = %s kW" % H2_cap)
        print("H2 tank = %s m3" % H2_tank_cap)
        print("Genset = %s kW" % Genset_cap)
        print("Total cost = %s" % -obj)

        self.optimal_sizing["PV"] = PV_cap
        self.optimal_sizing["INV"] = Inv_cap
        self.optimal_sizing["BAT"] = Battery_cap
        self.optimal_sizing["BATRVST"] = total_battery_reinvestment
        self.optimal_sizing["H2"] = H2_cap
        self.optimal_sizing["H2TANK"] = H2_tank_cap
        self.optimal_sizing["GEN"] = Genset_cap
        self.optimal_sizing["NPV"] = -obj

        pv_production = [0.0] * self.number_periods
        max_pv_production = [0.0] * self.number_periods
        load = self.total_consumption
        pv_curtailment = [0.0] * self.number_periods
        usable_capacity = [0.0] * self.number_periods
        soc = [0.0] * self.number_periods
        h2_energy = [0.0] * self.number_periods
        genset_production = [0.0] * self.number_periods
        g_import = [0.0] * self.number_periods
        g_export = [0.0] * self.number_periods
        discharge = [0.0] * self.number_periods
        charge = [0.0] * self.number_periods
        for p in range(self.number_periods):
            for g in self.entity.non_steerable_generators:
                pv_production[p] += self.model.non_steerable_generations[g, p].value
                pv_curtailment[p] += self.model.curtail[g, p].value
                max_pv_production[p] += self.non_steerable_generation_per_kW[g][p] * self.model.non_steerable_generators_capacity[g].value
            for s in self.entity.storages:
                soc[p] += self.model.soc[s, p].value
                usable_capacity[p] += self.model.usable_storages_capacity[s, p].value
                charge[p] += self.model.charge[s, p].value
                discharge[p] += - self.model.discharge[s, p].value
            for h in self.entity.h2_storages:
                h2_energy[p] += self.model.h2_energy_stored[h, p].value
            for g in self.entity.steerable_generators:
                genset_production[p] += self.model.steerable_generations[g, p].value
            g_import[p] = - self.model.imp_grid[p].value
            g_export[p] = self.model.exp_grid[p].value

        # x = range(self.number_periods)
        # plt.figure()
        # plt.plot(x, pv_production, color="tab:green", linewidth=2, label="PV production")
        # plt.plot(x, pv_curtailment, color="tab:blue", linewidth=2, label="PV curtailment")
        # # plt.plot(x, max_pv_production, color="tab:grey", linewidth=2, label="Max PV output")
        # plt.plot(x, load, color="tab:red", linewidth=2, label="Load")
        # plt.xlabel('Hours')
        # plt.ylabel('Power [kW]')
        # plt.grid()
        # plt.legend()
        # plt.figure()
        # plt.plot(x, g_import, color="tab:red", linewidth=2, label="Grid import")
        # plt.plot(x, g_export, color="tab:blue", linewidth=2, label="Grid export")
        # plt.xlabel('Hours')
        # plt.ylabel('Power [kW]')
        # plt.grid()
        # plt.legend()
        # plt.figure()
        # plt.plot(x, soc, color="tab:blue", linewidth=2, label="Storage SOC")
        # plt.plot(x, usable_capacity, color="tab:red", linewidth=2, label="Storage usable capacity")
        # plt.xlabel('Hours')
        # plt.ylabel('Capacity [kWh]')
        # plt.grid()
        # plt.legend()
        # plt.figure()
        # plt.plot(x, charge, color="tab:blue", linewidth=2, label="Battery charge")
        # plt.plot(x, discharge, color="tab:red", linewidth=2, label="Battery discharge")
        # plt.xlabel('Hours')
        # plt.ylabel('Power [kW]')
        # plt.grid()
        # plt.legend()
        # plt.figure()
        # plt.plot(x, h2_energy, color="tab:blue", linewidth=2, label="H2 stored energy")
        # plt.xlabel('Hours')
        # plt.ylabel('Capacity [kWh]')
        # plt.grid()
        # plt.legend()
        # plt.figure()
        # plt.plot(x, genset_production, color="tab:blue", linewidth=2, label="Steerable generation")
        # plt.xlabel('Hours')
        # plt.ylabel('Power [kW]')
        # plt.grid()
        # plt.legend()
        # plt.show()

        # sys.exit("STOP")

        return results, -obj


    # def optimize(self, maximum_iterations):
    #     """
    #     Define the optimal size of each device in the system.

    #     :param maximum_iterations: the max number of iterations performed by the optimization algorithm
    #     :return: Microgrid configuration at the optimum size.
    #     """
    #     self.max_iterations = maximum_iterations
    #     self.start_time = time.time()
    #     x_opt, _ = self._optimize_function(self._sized_device_lower_bounds, self._sized_device_upper_bounds,
    #                                        maximum_iterations=maximum_iterations)

    #     obj_opt = float(_)
    #     # Display final parameters
    #     for i, name in enumerate(self._sized_device_names):
    #         logging.info('Optimal size for device %s: %.2f(kW)' % (name, x_opt[i]))
    #     if not self.sizing_config.only_size:
    #         return self._x_to_microgrid(x_opt)