import json
import os
from multiprocessing.pool import Pool
from urllib.parse import urlparse

from flask import render_template, redirect, request, flash, abort, Response, session
from flask_caching import Cache

import captcha
import config
import od_util
from common import db, taskManager, searchEngine, logger, require_role
from database import Website
from search.search import InvalidQueryException
from tasks import Task


def setup_views(app):
    cache = Cache(app, config={'CACHE_TYPE': 'simple'})

    @app.route("/dl")
    @cache.cached(120)
    def downloads():
        # Get content of downloads directory
        dl_dir = "static/downloads/"
        dir_content = os.listdir(dl_dir)

        # Make paths relative to working directory
        # Only allow csv files
        files = [
            (name, os.path.join(dl_dir, name))
            for name in dir_content
            if name.find(".csv") != -1
        ]

        # Stat files
        # Remove any dirs placed accidentally
        files = [
            (f, full, os.stat(full))
            for f, full in files
            if os.path.isfile(full)
        ]

        if len(files) == 0:
            logger.warning("No export file to display in /dl")

        return render_template("downloads.html", export_file_stats=files)

    @app.route("/stats")
    @cache.cached(120)
    def stats_page():
        return render_template("stats.html")

    @app.route("/stats/json_chart")
    @cache.cached(240)
    def stats_json():
        stats = searchEngine.get_global_stats()
        if stats:
            db.join_website_on_stats(stats)
            return Response(json.dumps(stats), mimetype="application/json")
        return abort(500)

    @app.route("/website/<int:website_id>/")
    def website_info(website_id):
        website = db.get_website_by_id(website_id)

        if website:
            return render_template("website.html", website=website)
        else:
            abort(404)

    @app.route("/website/<int:website_id>/json_chart")
    @cache.memoize(60)
    def website_json_chart(website_id):
        website = db.get_website_by_id(website_id)

        if website:
            stats = searchEngine.get_stats(website_id)
            stats["base_url"] = website.url
            stats["report_time"] = website.last_modified
            return Response(json.dumps(stats), mimetype="application/json")
        else:
            abort(404)

    @app.route("/website/<int:website_id>/links")
    def website_links(website_id):
        website = db.get_website_by_id(website_id)

        if website:
            links = searchEngine.get_link_list(website_id, website.url)
            return Response("\n".join(links), mimetype="text/plain")
        else:
            abort(404)

    @app.route("/website/")
    def websites():
        page = int(request.args.get("p")) if "p" in request.args else 0
        url = request.args.get("url") if "url" in request.args else ""
        if url:
            parsed_url = urlparse(url)
            if parsed_url.scheme:
                search_term = (parsed_url.scheme + "://" + parsed_url.netloc)
            else:
                flash("Sorry, I was not able to parse this url format. "
                      "Make sure you include the appropriate scheme (http/https/ftp)", "warning")
                search_term = ""
        else:
            search_term = url

        return render_template("websites.html",
                               websites=db.get_websites(50, page, search_term),
                               p=page, url=search_term, per_page=50)

    @app.route("/website/random")
    def random_website():
        return redirect("/website/" + str(db.get_random_website_id()))

    @app.route("/website/<int:website_id>/clear")
    def admin_clear_website(website_id):
        require_role("admin")

        searchEngine.delete_docs(website_id)
        flash("Cleared all documents associated with this website", "success")
        return redirect("/website/" + str(website_id))

    @app.route("/website/<int:website_id>/delete")
    def admin_delete_website(website_id):
        require_role("admin")

        searchEngine.delete_docs(website_id)
        db.delete_website(website_id)
        flash("Deleted website " + str(website_id), "success")
        return redirect("/website/")

    @app.route("/website/<int:website_id>/rescan")
    def admin_rescan_website(website_id):
        require_role("admin")
        website = db.get_website_by_id(website_id)

        if website:
            priority = request.args.get("priority") if "priority" in request.args else 1
            task = Task(website_id, website.url, priority)
            taskManager.queue_task(task)

            flash("Enqueued rescan task", "success")
        else:
            flash("Website does not exist", "danger")
        return redirect("/website/" + str(website_id))

    @app.route("/search")
    def search():
        q = request.args.get("q") if "q" in request.args else ""
        sort_order = request.args.get("sort_order") if "sort_order" in request.args else "score"

        page = request.args.get("p") if "p" in request.args else "0"
        page = int(page) if page.isdigit() else 0

        per_page = request.args.get("per_page") if "per_page" in request.args else "50"
        per_page = int(per_page) if per_page.isdigit() else "50"
        per_page = per_page if per_page in config.RESULTS_PER_PAGE else 50

        extensions = request.args.get("ext") if "ext" in request.args else None
        extensions = [ext.strip().strip(".").lower() for ext in extensions.split(",")] if extensions else []

        size_min = request.args.get("size_min") if "size_min" in request.args else "size_min"
        size_min = int(size_min) if size_min.isdigit() else 0
        size_max = request.args.get("size_max") if "size_max" in request.args else "size_max"
        size_max = int(size_max) if size_max.isdigit() else 0

        date_min = request.args.get("date_min") if "date_min" in request.args else "date_min"
        date_min = int(date_min) if date_min.isdigit() else 0
        date_max = request.args.get("date_max") if "date_max" in request.args else "date_max"
        date_max = int(date_max) if date_max.isdigit() else 0

        match_all = "all" in request.args

        field_name = "field_name" in request.args
        field_trigram = "field_trigram" in request.args
        field_path = "field_path" in request.args

        if not field_name and not field_trigram and not field_path:
            # If no fields are selected, search in all
            field_name = field_path = field_trigram = True

        fields = []
        if field_path:
            fields.append("path")
        if field_name:
            fields.append("name^5")
        if field_trigram:
            fields.append("name.nGram^2")

        if len(q) >= 3:

            blocked = False
            hits = None
            if not config.CAPTCHA_SEARCH or captcha.verify():

                try:
                    hits = searchEngine.search(q, page, per_page, sort_order,
                                               extensions, size_min, size_max, match_all, fields, date_min, date_max)
                    hits = db.join_website_on_search_result(hits)
                except InvalidQueryException as e:
                    flash("<strong>Invalid query:</strong> " + str(e), "warning")
                    blocked = True
                except:
                    flash("Query failed, this could mean that the search server is overloaded or is not reachable. "
                          "Please try again later", "danger")

                results = hits["hits"]["total"] if hits else -1
                took = hits["took"] if hits else -1
                forwarded_for = request.headers["X-Forwarded-For"] if "X-Forwarded-For" in request.headers else None

                logger.info("SEARCH '{}' [res={}, t={}, p={}x{}, ext={}] by {}{}"
                            .format(q, results, took, page, per_page, str(extensions),
                                    request.remote_addr, "_" + forwarded_for if forwarded_for else ""))

                db.log_search(request.remote_addr, forwarded_for, q, extensions, page, blocked, results, took)
                if blocked:
                    return redirect("/search")
            else:
                flash("<strong>Error:</strong> Invalid captcha please try again", "danger")

        else:
            hits = None

        return render_template("search.html",
                               results=hits,
                               q=q,
                               p=page, per_page=per_page,
                               sort_order=sort_order,
                               results_set=config.RESULTS_PER_PAGE,
                               extensions=",".join(extensions),
                               size_min=size_min, size_max=size_max,
                               match_all=match_all,
                               field_trigram=field_trigram, field_path=field_path, field_name=field_name,
                               date_min=date_min, date_max=date_max,
                               show_captcha=config.CAPTCHA_SEARCH, captcha=captcha)

    @app.route("/contribute")
    @cache.cached(600)
    def contribute():
        return render_template("contribute.html")

    @app.route("/")
    def home():
        try:
            stats = searchEngine.get_global_stats()
            stats["website_count"] = len(db.get_all_websites())
        except:
            stats = {}
        return render_template("home.html", stats=stats,
                               show_captcha=config.CAPTCHA_SEARCH, captcha=captcha)

    @app.route("/submit")
    def submit():
        return render_template("submit.html", captcha=captcha, show_captcha=config.CAPTCHA_SUBMIT)

    def try_enqueue(url):
        url = os.path.join(url, "")
        url = od_util.get_top_directory(url)

        if not od_util.is_valid_url(url):
            return "<strong>Error:</strong> Invalid url. Make sure to include the appropriate scheme.", "warning"

        website = db.get_website_by_url(url)
        if website:
            return "Website already exists", "danger"

        website = db.website_exists(url)
        if website:
            return "A parent directory of this url has already been posted", "danger"

        if db.is_blacklisted(url):
            return "<strong>Error:</strong> " \
                   "Sorry, this website has been blacklisted. If you think " \
                   "this is an error, please <a href='/contribute'>contact me</a>.", "danger"

        if not od_util.is_od(url):
            return "<strong>Error:</strong>" \
                   "The anti-spam algorithm determined that the submitted url is not " \
                   "an open directory or the server is not responding. If you think " \
                   "this is an error, please <a href='/contribute'>contact me</a>.", "danger"

        website_id = db.insert_website(Website(url, str(request.remote_addr + "_" +
                                                        request.headers.get("X-Forwarded-For", "")),
                                               request.user_agent))

        task = Task(website_id, url, priority=1)
        taskManager.queue_task(task)

        return "The website has been added to the queue", "success"

    @app.route("/enqueue", methods=["POST"])
    def enqueue():
        if not config.CAPTCHA_SUBMIT or captcha.verify():

            url = os.path.join(request.form.get("url"), "")
            message, msg_type = try_enqueue(url)
            flash(message, msg_type)

            return redirect("/submit")

        else:
            flash("<strong>Error:</strong> Invalid captcha please try again", "danger")
            return redirect("/submit")

    def check_url(url):
        url = os.path.join(url, "")
        try_enqueue(url)
        return None

    @app.route("/enqueue_bulk", methods=["POST"])
    def enqueue_bulk():
        if not config.CAPTCHA_SUBMIT or captcha.verify():

            urls = request.form.get("urls")
            if urls:
                urls = urls.split()

                if 0 < len(urls) <= 1000:  # TODO: Load from config & adjust placeholder/messages?

                    pool = Pool(processes=6)
                    pool.map(func=check_url, iterable=urls)
                    pool.close()

                    flash("Submitted websites to the queue", "success")

                    return redirect("/submit")

                else:
                    flash("Too few or too many urls, please submit 1-10 urls", "danger")
                    return redirect("/submit")
            else:
                flash("Too few or too many urls, please submit 1-10 urls", "danger")
                return redirect("/submit")
        else:
            flash("<strong>Error:</strong> Invalid captcha please try again", "danger")
            return redirect("/submit")

    @app.route("/admin")
    def admin_login_form():
        if "username" in session:
            return redirect("/dashboard")
        return render_template("admin.html", captcha=captcha, show_captcha=config.CAPTCHA_LOGIN)

    @app.route("/login", methods=["POST"])
    def admin_login():
        if not config.CAPTCHA_LOGIN or captcha.verify():

            username = request.form.get("username")
            password = request.form.get("password")

            if db.check_login(username, password):
                session["username"] = username
                flash("Logged in", "success")
                return redirect("/dashboard")

            flash("Invalid username/password combo", "danger")
            return redirect("/admin")

        else:
            flash("Invalid captcha", "danger")
            return redirect("/admin")

    @app.route("/logout")
    def admin_logout():
        session.clear()
        flash("Logged out", "info")
        return redirect("/")

    @app.route("/dashboard")
    def admin_dashboard():
        require_role("admin")
        tokens = db.get_tokens()
        blacklist = db.get_blacklist()

        return render_template("dashboard.html", api_tokens=tokens, blacklist=blacklist)

    @app.route("/blacklist/add", methods=["POST"])
    def admin_blacklist_add():
        require_role("admin")
        url = request.form.get("url")
        db.add_blacklist_website(url)
        flash("Added item to blacklist", "success")
        return redirect("/dashboard")

    @app.route("/blacklist/<int:blacklist_id>/delete")
    def admin_blacklist_remove(blacklist_id):
        require_role("admin")
        db.remove_blacklist_website(blacklist_id)
        flash("Removed blacklist item", "success")
        return redirect("/dashboard")

    @app.route("/generate_token", methods=["POST"])
    def admin_generate_token():
        require_role("admin")
        description = request.form.get("description")

        db.generate_api_token(description)
        flash("Generated API token", "success")

        return redirect("/dashboard")

    @app.route("/del_token", methods=["POST"])
    def admin_del_token():
        require_role("admin")
        token = request.form.get("token")

        db.delete_token(token)
        flash("Deleted API token", "success")
        return redirect("/dashboard")
