delete from server y where y.cluster_id = (select c.id from cluster c where c.name = %(cluster_name)s )