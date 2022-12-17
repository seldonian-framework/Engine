import numpy as np
from scipy.ndimage import uniform_filter1d
import matplotlib.pyplot as plt
from functools import reduce

def plot_gradient_descent(
	solution,
	primary_objective_name,
	save=False,
	savename='test.png',
	show=True):
	""" Make figure showing evolution of gradient descent.
	One row per constraint. The primary objective and lagrangian
	subplots are repeated in each row because they are not
	changing with the constraint

	Plots:
	i) primary objective
	ii) lagrange multipliers for each constraint, lambda_i
	iii) each constraint function, g_i
	iv) Lagranian L = f + sum_i^n {lambda_i*g_i}

	:param solution: The solution dictionary returned by gradient descent
	:type solution: dict
	:param primary_objective_name: The label you want displayed on the plot
		for the primary objective
	:type primary_objective_name: str
	:param save: Whether to save the plot
	:type save: bool
	:param savename: The full path where you want to save the plot
	:type savename: str
	:param show: Whether to show the plot with plt.show().
		Only relevant when save=False
	:type show: bool
	"""
	
	fontsize=10
	f_vals = solution['f_vals'] # length = i array where i is number of iterations 
	g_vals = solution['g_vals'] # i x j array where i is number of iterations, j is number of constraints
	lamb_vals = solution['lamb_vals'] # i x j array where i is number of iterations, j is number of constraints
	L_vals = solution['L_vals']
	masks = []
	for q in [f_vals,g_vals,lamb_vals,L_vals]:
		masks.append(np.squeeze(np.isfinite(q)))
	final_mask = reduce(np.logical_and,masks)
	f_vals_masked = f_vals[final_mask]
	g_vals_masked = g_vals[final_mask]
	lamb_vals_masked = np.array(lamb_vals)[final_mask]
	L_vals_masked = np.array(L_vals)[final_mask]
	its = np.arange(len(f_vals))
	its_masked = its[final_mask]
	# Running average f and L
	f_runavg = uniform_filter1d(f_vals_masked,size=15)
	best_index = solution['best_index']
	best_f = solution['best_f']
	best_g = solution['best_g']
	n_constraints = g_vals_masked[0].shape[0]
	n_cols = 4 
	# fig = plt.figure(figsize=(10,4+(n_constraints-1)*2))
	fig,axes = plt.subplots(n_constraints,4,figsize=(10,4+(n_constraints-1)*2))
	# 1 row per constraint, f and L subplots repeated in each row
	for constraint_index in range(n_constraints): # 0,1,2,...
		row_number = constraint_index + 1 # 1,2,3,...

		if n_constraints > 1:
			axes_this_constraint = axes[constraint_index]
		else:
			axes_this_constraint = axes
		
		# Subplot: Primary objective, repeated for each row
		ax_f = axes_this_constraint[0]
		ax_f.plot(its_masked,f_vals_masked,linewidth=2)
		ax_f.plot(its_masked,f_runavg,linewidth=1)
		ax_f.set_xlabel("Iteration")

		ax_f.set_ylabel(rf"$\hat{{f}}(\theta,D_\mathrm{{cand}})$: {primary_objective_name}",fontsize=fontsize)
		ax_f.axvline(x=best_index,linestyle='--',color='k')
		ax_f.axhline(y=best_f,linestyle='--',color='k')

		# Subplot: lambda[constraint_index]
		ax_lamb = axes_this_constraint[1]
		lamb_vals_this_constraint = [x[constraint_index] for x in lamb_vals_masked]
		ax_lamb.plot(its_masked,lamb_vals_this_constraint,
			linewidth=2)
		ax_lamb.set_xlabel("Iteration")
		ax_lamb.set_ylabel(rf"$\lambda_{row_number}$",fontsize=fontsize)

		ax_lamb.axvline(x=best_index,linestyle='--',color='k')
		best_lamb = lamb_vals_masked[best_index][constraint_index]
		ax_lamb.axhline(y=best_lamb,linestyle='--',color='k')

		# Subplot: g[constraint_index]
		ax_g = axes_this_constraint[2]
		g_vals_this_constraint = [x[constraint_index] for x in g_vals_masked]
		ax_g.plot(its_masked,g_vals_this_constraint,linewidth=2)
		ax_g.set_xlabel("Iteration")
		ax_g.set_ylabel(rf"$\mathrm{{HCUB}}(\hat{{g}}_{{{row_number}}}(\theta,D_\mathrm{{cand}}))$",fontsize=fontsize)
		ax_g.fill_between(np.arange(len(g_vals_masked)),0.0,1e6,color='r',zorder=0,alpha=0.5)
		ax_g.set_ylim(min(-0.25,min(g_vals_this_constraint)),max(0.25,max(g_vals_this_constraint)*1.2))
		ax_g.axvline(x=best_index,linestyle='--',color='k')
		ax_g.axhline(y=best_g[constraint_index],linestyle='--',color='k')
		ax_g.set_xlim(0,len(g_vals_masked))

		# Subplot: Lagrangian, repeated for each row
		ax_L = axes_this_constraint[3]
		ax_L.plot(its_masked,L_vals_masked,linewidth=2)
		ax_L.set_xlabel("Iteration")
		ax_L.set_ylabel(r"$L(\theta,\lambda)$",fontsize=fontsize)
		ax_L.axvline(x=best_index,linestyle='--',color='k')
		best_L = L_vals_masked[best_index]
		ax_L.axhline(y=best_L,linestyle='--',color='k')
	title = rf"KKT optimization for $L(\theta,\lambda) = \hat{{f}}(\theta,D_\mathrm{{cand}}) + \sum_{{k=1}}^{{{n_constraints}}}{{\lambda_k}} \mathrm{{HCUB}}(\hat{{g}}_k(\theta,D_\mathrm{{cand}})) $"
	plt.suptitle(title)
	plt.tight_layout()
	if save:
		plt.savefig(savename,dpi=300)
		print(f"Saved {savename}")
	else:
		if show:
			plt.show()
	return fig